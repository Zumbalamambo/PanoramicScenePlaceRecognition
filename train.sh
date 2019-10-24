python PlaceRecognitionTrain.py \
--dataset=highway \
--mode=train \
--resume=checkpoints_res \
--savePath=checkpoints_res_0 \
--ckpt=latest \
--arch=resnet18 \
--numTrain=2 \
--weightDecay=0.001 \
--cacheBatchSize=224 \
--batchSize=6 \
--threads=4 \
--nEpochs=50 \
--start-epoch=22 \
--cacheRefreshRate=500 \
--lrStep=10 \
--lr=0.001
