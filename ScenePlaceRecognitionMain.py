# Test scene place recognition network with attention, which is applicable to unwrapped panoramic imagesfrom __future__ import print_functionimport argparseimport randomfrom os.path import join, existsimport osimport torchimport torch.nn as nnimport torch.nn.functional as Ffrom torch.utils.data import DataLoaderfrom PIL import Imageimport h5pyimport faissimport numpy as npfrom netVLAD import netvladfrom torchvision import transforms as trnfrom torch.autograd import Variable as Vfrom scipy.misc import imresize as imresizeimport cv2import SceneModelimport warningsparser = argparse.ArgumentParser(description='ScenePlaceRecognitionTest')parser.add_argument('--cacheBatchSize', type=int, default=4, help='Batch size for caching and testing')parser.add_argument('--nGPU', type=int, default=1, help='number of GPU to use.')parser.add_argument('--nocuda', action='store_true', help='Dont use cuda')parser.add_argument('--threads', type=int, default=8, help='Number of threads for each data loader to use')parser.add_argument('--seed', type=int, default=123, help='Random seed to use.')parser.add_argument('--dataPath', type=str, default='data/', help='Path for centroid data.')parser.add_argument('--cachePath', type=str, default='/tmp/', help='Path to save cache to.')parser.add_argument('--resume', type=str, default='/',                    help='Path to load checkpoint from, for resuming training or testing.')parser.add_argument('--ckpt', type=str, default='best',                    help='Resume from latest or best checkpoint.', choices=['latest', 'best'])parser.add_argument('--dataset', type=str, default='MOLP',                    help='Dataset to use', choices=['MOLP', 'Yuquan'])parser.add_argument('--pooling', type=str, default='netvlad', help='type of pooling to use',                    choices=['netvlad', 'max', 'avg'])parser.add_argument('--num_clusters', type=int, default=64, help='Number of NetVlad clusters. Default=64')parser.add_argument('--attention', action='store_true', help='Whether with the attention module.')parser.add_argument('--netVLADtrainNum', type=int, default=2, help='Number of trained blocks in Resnet18.')parser.add_argument('--panoramicCrop', type=int, default=4, help='Number of panoramic crops')def load_labels():    # prepare all the labels    # scene category relevant    file_name_category = 'Place365/categories_places365.txt'    classes = list()    with open(file_name_category) as class_file:        for line in class_file:            classes.append(line.strip().split(' ')[0][3:])    classes = tuple(classes)    # indoor and outdoor relevant    file_name_IO = 'Place365/IO_places365.txt'    with open(file_name_IO) as f:        lines = f.readlines()        labels_IO = []        for line in lines:            items = line.rstrip().split()            labels_IO.append(int(items[-1]) -1) # 0 is indoor, 1 is outdoor    labels_IO = np.array(labels_IO)    # scene attribute relevant    file_name_attribute = 'Place365/labels_sunattribute.txt'    with open(file_name_attribute) as f:        lines = f.readlines()        labels_attribute = [item.rstrip() for item in lines]    file_name_W = 'Place365/W_sceneattribute_wideresnet18.npy'    W_attribute = np.load(file_name_W)    return classes, labels_IO, labels_attribute, W_attributedef returnTF():#load the image transformer    tf = trn.Compose([        trn.Resize((224,224)),        trn.ToTensor(),        trn.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])    ])    return tfdef returnCAM(feature_conv, weight_softmax, class_idx):    # generate the class activation maps upsample to 256x256    size_upsample = (256, 256)    nc, h, w = feature_conv.shape    output_cam = []    for idx in class_idx:        cam = weight_softmax[class_idx].dot(feature_conv.reshape((nc, h*w)))        cam = cam.reshape(h, w)        cam = cam - np.min(cam)        cam_img = cam / np.max(cam)        cam_img = np.uint8(255 * cam_img)        output_cam.append(imresize(cam_img, size_upsample))    return output_camdef testDataset(eval_set, outputFeats=False):    # TODO what if features dont fit in memory?    test_data_loader = DataLoader(dataset=eval_set,                                  num_workers=opt.threads, batch_size=opt.cacheBatchSize, shuffle=False,                                  pin_memory=cuda)    # 不会反向传播，提高inference速度    with torch.no_grad():        print('====> Extracting Features')        pool_size = encoder_dim        if opt.pooling.lower() == 'netvlad': pool_size *= opt.num_clusters        dbFeat = np.empty((len(eval_set), pool_size))        for iteration, (input, indices) in enumerate(test_data_loader, 1):            input_batches = torch.empty(opt.panoramicCrop, 3, 224, 224)            for idx in range(opt.panoramicCrop):                input_batches[idx, :, :, 0:224] = input[0, :, :, idx*224:(idx+1)*224]            input_batches = input_batches.to(device)            # forward pass - add scene information            logit = modelPlaces.forward(input_batches)            # forward pass - netvlad            image_encoding = model.encoder(SceneModel.netVLADlayer_input[0])            SceneModel.netVLADlayer_input.clear()            vlad_encoding_batches = model.pool(image_encoding)            # 将各个batch相加            vlad_encoding = vlad_encoding_batches[0, :]            if vlad_encoding.shape[0] > 1:                for ii in range(1, vlad_encoding_batches.shape[0]):                    vlad_encoding.add_(vlad_encoding_batches[ii, :])            dbFeat[indices.detach().numpy(), :] = vlad_encoding.detach().cpu().numpy()            if iteration % 50 == 0 or len(test_data_loader) <= 10:                print("==> Batch ({}/{})".format(iteration, len(test_data_loader)), flush=True)            del input, input_batches, logit, vlad_encoding, image_encoding, vlad_encoding_batches    del test_data_loader    # extracted for both db and query, now split in own sets    qFeat = dbFeat[eval_set.numDb:].astype('float32')    dbFeat = dbFeat[:eval_set.numDb].astype('float32')    if outputFeats:        np.savetxt("query.txt", qFeat)        np.savetxt("database.txt", dbFeat)    print('====> Building faiss index')    faiss_index = faiss.IndexFlatL2(pool_size)    faiss_index.add(dbFeat)    print('====> Calculating recall @ N')    n_values = [1, 5, 10, 20]    distances, predictions = faiss_index.search(qFeat, max(n_values))    # for each query get those within threshold distance    gt = eval_set.getPositives()    correct_at_n = np.zeros(len(n_values))    # TODO can we do this on the matrix in one go?    for qIx, pred in enumerate(predictions):        for i, n in enumerate(n_values):            # if in top N then also in top NN, where NN > N            if np.any(np.in1d(pred[:n], gt[qIx])):                correct_at_n[i:] += 1                break    recall_at_n = correct_at_n / eval_set.numQ    recalls = {}  # make dict for output    for i, n in enumerate(n_values):        recalls[n] = recall_at_n[i]        print("====> Recall@{}: {:.4f}".format(n, recall_at_n[i]))    # fp = 0    # tp = 0    # for i in range(0, len(predictions)):    #     if abs(predictions[i] - i)<=5:    #         tp=tp+1    #     else:    #         fp=fp+1    #    # precision = tp/(tp+fp)    # recall = 1    # f1=2*precision/(precision+recall)    #    # print(['F1=', f1 ])    return recalls, distances, predictionsdef test():    # load the labels    classes, labels_IO, labels_attribute, W_attribute = load_labels()    # load the model 已经加载完毕    # load the transformer    tf = returnTF()  # image transformer    # get the softmax weight    params = list(modelPlaces.parameters())    weight_softmax = params[-2].data.numpy()    weight_softmax[weight_softmax < 0] = 0    # load the test image    img_url = 'http://places.csail.mit.edu/demo/5.jpg'    os.system('wget %s -q -O test.jpg' % img_url)    img = Image.open('test.jpg')    input_img = V(tf(img).unsqueeze(0))    # forward pass    logit = modelPlaces.forward(input_img)    h_x = F.softmax(logit, 1).data.squeeze()    probs, idx = h_x.sort(0, True)    probs = probs.numpy()    idx = idx.numpy()    print('RESULT ON ' + img_url)    # output the IO prediction    io_image = np.mean(labels_IO[idx[:10]])  # vote for the indoor or outdoor    if io_image < 0.5:        print('--TYPE OF ENVIRONMENT: indoor')    else:        print('--TYPE OF ENVIRONMENT: outdoor')    # output the prediction of scene category    print('--SCENE CATEGORIES:')    for i in range(0, 5):        print('{:.3f} -> {}'.format(probs[i], classes[idx[i]]))    # output the scene attributes    responses_attribute = W_attribute.dot(SceneModel.features_blobs[1])    idx_a = np.argsort(responses_attribute)    print('--SCENE ATTRIBUTES:')    print(', '.join([labels_attribute[idx_a[i]] for i in range(-1, -10, -1)]))    # generate class activation mapping    print('Class activation map is saved as cam.jpg')    CAMs = returnCAM(SceneModel.features_blobs[0], weight_softmax, [idx[0]])    # render the CAM and output    img = cv2.imread('test.jpg')    height, width, _ = img.shape    heatmap = cv2.applyColorMap(cv2.resize(CAMs[0], (width, height)), cv2.COLORMAP_JET)    result = heatmap * 0.4 + img * 0.5    cv2.imwrite('cam.jpg', result)class Flatten(nn.Module):    def forward(self, input):        return input.view(input.size(0), -1)class L2Norm(nn.Module):    def __init__(self, dim=1):        super().__init__()        self.dim = dim    def forward(self, input):        return F.normalize(input, p=2, dim=self.dim)if __name__ == "__main__":    # ignore warnings -- UserWarning: Loky-backed parallel loops cannot be called in a multiprocessing, setting n_jobs=1    warnings.filterwarnings("ignore")    opt = parser.parse_args()    # designate device    cuda = not opt.nocuda    if cuda and not torch.cuda.is_available():        raise Exception("No GPU found, please run with --nocuda")    device = torch.device("cuda" if cuda else "cpu")    random.seed(opt.seed)    np.random.seed(opt.seed)    torch.manual_seed(opt.seed)    if cuda:        torch.cuda.manual_seed(opt.seed)    # designate dataset    if opt.dataset.lower() == 'molp':        from netVLAD import MOLP as dataset    elif opt.dataset.lower() == 'yuquan':        from netVLAD import Yuquan as dataset    else:        raise Exception('Unknown dataset')    print('===> Loading dataset(s)')    whole_test_set = dataset.get_whole_val_set(opt.panoramicCrop)    print('====> Query count:', whole_test_set.numQ)    # build network architecture: ResNet-18 with scene classification / scene attribute    print('===> Building model')    modelPlaces = SceneModel.loadSceneRecognitionModel(opt.netVLADtrainNum)    modelPlaces = modelPlaces.to(device)    # build network architecture: ResNet-18 with place recognition    model = SceneModel.loadPlaceRecognitionEncoder(opt.netVLADtrainNum)    # 添加（初始化）pooling模块    encoder_dim = 512    if opt.pooling.lower() == 'netvlad':        net_vlad = netvlad.NetVLAD(num_clusters=opt.num_clusters, dim=encoder_dim, vladv2=False)        initcache = join(opt.dataPath, 'centroids', 'resnet18_pitts30k_' + str(opt.num_clusters) + '_desc_cen.hdf5')        if not exists(initcache):            raise FileNotFoundError('Could not find clusters, please run with --mode=cluster before proceeding')        with h5py.File(initcache, mode='r') as h5:            clsts = h5.get("centroids")[...]            traindescs = h5.get("descriptors")[...]            net_vlad.init_params(clsts, traindescs)            del clsts, traindescs        model.add_module('pool', net_vlad)    elif opt.pooling.lower() == 'max':        global_pool = nn.AdaptiveMaxPool2d((1, 1))        model.add_module('pool', nn.Sequential(*[global_pool, Flatten(), L2Norm()]))    elif opt.pooling.lower() == 'avg':        global_pool = nn.AdaptiveAvgPool2d((1, 1))        model.add_module('pool', nn.Sequential(*[global_pool, Flatten(), L2Norm()]))    else:        raise ValueError('Unknown pooling type: ' + opt.pooling)    isParallel = False    if opt.nGPU > 1 and torch.cuda.device_count() > 1:        model.encoder = nn.DataParallel(model.encoder)        model.pool = nn.DataParallel(model.pool)        isParallel = True    # load the paramters of the netVLAD branch    if opt.ckpt.lower() == 'latest':        resume_ckpt = join(opt.resume, 'checkpoints', 'checkpoint.pth.tar')    elif opt.ckpt.lower() == 'best':        resume_ckpt = join(opt.resume, 'checkpoints', 'model_best.pth.tar')    model = SceneModel.loadNetVLADParams(resume_ckpt, opt.netVLADtrainNum, model)    model = model.to(device)    # execute test / cluster    print('===> Running evaluation step')    epoch = 1    #test()    _, distances, predictions = testDataset(whole_test_set)