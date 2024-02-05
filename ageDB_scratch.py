import argparse
import os
import sys
import logging
import torch
import time
from models import SupResNet
from dataset import *
from utils import *
from loss import *
import numpy as np
from sklearn.metrics import r2_score
from scipy import stats


def parse_option():
    parser = argparse.ArgumentParser('argument for training')

    parser.add_argument('--print_freq', type=int, default=10, help='print frequency')
    parser.add_argument('--save_freq', type=int, default=100, help='save frequency')
    parser.add_argument('--save_curr_freq', type=int, default=1, help='save curr last frequency')

    parser.add_argument('--batch_size', type=int, default=256, help='batch_size')
    parser.add_argument('--num_workers', type=int, default=1, help='num of workers to use')
    parser.add_argument('--epochs', type=int, default=400, help='number of training epochs')
    parser.add_argument('--learning_rate', type=float, default=0.2, help='learning rate')
    parser.add_argument('--lr_decay_rate', type=float, default=0.1, help='decay rate for learning rate')
    parser.add_argument('--weight_decay', type=float, default=1e-4, help='weight decay')
    parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
    parser.add_argument('--alpha', type=float, default=1.0, help='alpha parameter for FAR')
    parser.add_argument('--trial', type=str, default='0', help='id for recording multiple runs')
    parser.add_argument('--loss', type=str, default='FAR', help='loss type')

    parser.add_argument('--data_folder', type=str, default='../Rank-N-Contrast/data', help='path to custom dataset')
    parser.add_argument('--dataset', type=str, default='AgeDB', choices=['AgeDB'], help='dataset')
    parser.add_argument('--model', type=str, default='resnet18', choices=['resnet18', 'resnet50'])
    parser.add_argument('--resume', type=str, default='', help='resume ckpt path')
    parser.add_argument('--aug', type=str, default='crop,flip,color,grayscale', help='augmentations')

    opt = parser.parse_args()

    opt.model_path = './save/{}_models'.format(opt.dataset)
    opt.model_name = opt.loss+'_{}_{}_ep_{}_lr_{}_d_{}_wd_{}_alpha_{}_mmt_{}_bsz_{}_aug_{}_trial_{}'. \
        format(opt.dataset, opt.model, opt.epochs, opt.learning_rate, opt.lr_decay_rate, opt.weight_decay, opt.alpha, opt.momentum,
               opt.batch_size, opt.aug, opt.trial)
    if len(opt.resume):
        opt.model_name = opt.resume.split('/')[-2]

    opt.save_folder = os.path.join(opt.model_path, opt.model_name)
    if not os.path.isdir(opt.save_folder):
        os.makedirs(opt.save_folder)
    else:
        print('WARNING: folder exist.')

    logging.root.handlers = []
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(opt.save_folder, 'training.log')),
            logging.StreamHandler()
        ])

    print(f"Model name: {opt.model_name}")
    print(f"Options: {opt}")

    return opt


def set_loader(opt):
    train_transform = get_transforms(split='train', aug=opt.aug)
    val_transform = get_transforms(split='val', aug=opt.aug)
    print(f"Train Transforms: {train_transform}")
    print(f"Val Transforms: {val_transform}")

    train_dataset = globals()[opt.dataset](data_folder=opt.data_folder, transform=train_transform, split='train')
    #train_dataset = globals()[opt.dataset](
    #    data_folder=opt.data_folder,
    #    transform=TwoCropTransform(train_transform),
    #    split='train'
    #)
    val_dataset = globals()[opt.dataset](data_folder=opt.data_folder, transform=val_transform, split='val')
    test_dataset = globals()[opt.dataset](data_folder=opt.data_folder, transform=val_transform, split='test')

    print(f'Train set size: {train_dataset.__len__()}\t'
          f'Val set size: {val_dataset.__len__()}\t'
          f'Test set size: {test_dataset.__len__()}')

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=opt.num_workers, pin_memory=True
    )
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=opt.batch_size, shuffle=False, num_workers=opt.num_workers, pin_memory=True
    )
    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=opt.batch_size, shuffle=False, num_workers=opt.num_workers, pin_memory=True
    )

    return train_loader, val_loader, test_loader


def set_model(opt):
    model = SupResNet(name=opt.model, num_classes=get_label_dim(opt.dataset))
    if opt.loss in ['FAR', 'FAR-EXP']:
        criterion = FAR(alpha=opt.alpha, version=opt.loss)
    else:
        criterion = torch.nn.L1Loss()

    if torch.cuda.is_available():
        if torch.cuda.device_count() > 1:
            model.encoder = torch.nn.DataParallel(model.encoder)
        model = model.cuda()
        criterion = criterion.cuda()
        torch.backends.cudnn.benchmark = True

    return model, criterion


def train(train_loader, model, criterion, optimizer, epoch, opt):
    model.train()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    end = time.time()
    for idx, (images, labels) in enumerate(train_loader):
        data_time.update(time.time() - end)
        bsz = labels.shape[0]

        # images = torch.cat([images[0], images[1]], dim=0)
        # labels = labels.repeat(2, 1)  # [2bs, label_dim]
        if torch.cuda.is_available():
            images = images.cuda(non_blocking=True)
            labels = labels.cuda(non_blocking=True)

        output, feat = model(images)
        if opt.loss == 'ConR':
            loss = criterion(output, labels) + opt.alpha*ConR(feat, labels, output)
        elif opt.loss == 'ranksim':
            loss = criterion(output, labels) + batchwise_ranking_regularizer(feat, labels, opt.alpha)
        elif opt.loss == 'focal-l1':
            loss = weighted_focal_l1_loss(output, labels, beta=opt.alpha)
        elif opt.loss == 'focal-mse':
            loss = weighted_focal_mse_loss(output, labels, beta=opt.alpha)
        else:
            loss = criterion(output, labels)
        losses.update(loss.item(), bsz)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        batch_time.update(time.time() - end)
        end = time.time()

        if (idx + 1) % opt.print_freq == 0:
            to_print = 'Train: [{0}][{1}/{2}]\t'\
                       'BT {batch_time.val:.3f} ({batch_time.avg:.3f})\t'\
                       'DT {data_time.val:.3f} ({data_time.avg:.3f})\t'\
                       'loss {loss.val:.5f} ({loss.avg:.5f})'.format(
                epoch, idx + 1, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses
            )
            print(to_print)
            sys.stdout.flush()


def validate(val_loader, model):
    model.eval()

    pred = []
    truth = [] 
    with torch.no_grad():
        for idx, (images, labels) in enumerate(val_loader):
            images = images.cuda()
            labels = labels.cuda()

            output, feat = model(images)

            pred.append(output.cpu().detach().numpy())
            truth.append(labels.cpu().detach().numpy())
    pred = np.concatenate(pred, axis=0)
    truth = np.concatenate(truth, axis=0)
    va_MAE = np.abs(pred-truth).mean()
    va_RMSE = ((pred-truth)**2).mean()**0.5
    va_pear = np.corrcoef(truth, pred, rowvar=False)[0,1]
    va_spear = stats.spearmanr(truth, pred)[0]
    va_R2 = r2_score(truth, pred)
    return [va_MAE, va_RMSE, va_pear, va_spear, va_R2]


def main():
    opt = parse_option()

    # build data loader
    train_loader, val_loader, test_loader = set_loader(opt)

    # build model and criterion
    model, criterion = set_model(opt)

    # build optimizer
    optimizer = set_optimizer(opt, model)

    start_epoch = 1
    if len(opt.resume):
        ckpt_state = torch.load(opt.resume)
        model.load_state_dict(ckpt_state['model'])
        optimizer.load_state_dict(ckpt_state['optimizer'])
        start_epoch = ckpt_state['epoch'] + 1
        print(f"<=== Epoch [{ckpt_state['epoch']}] Resumed from {opt.resume}!")

    best_error = 1e10
    best_test = [best_error, best_error, best_error, best_error, best_error]
    save_file_best = os.path.join(opt.save_folder, 'best.pth')

    # training routine
    for epoch in range(start_epoch, opt.epochs + 1):
        adjust_learning_rate(opt, optimizer, epoch)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, opt)

        valid_error = validate(val_loader, model)
        print(valid_error)
        print('valid_MAE={:.3f}, valid_RMSE={:.3f}, valid_Pearson={:.3f}, valid_Spearman={:.3f}, valid_R2={:.3f}'.format(*valid_error))

        is_best = valid_error[0] < best_error
        best_error = min(valid_error[0], best_error)
        print(f"Best Error: {best_error:.3f}")
    
        test_error = validate(test_loader, model)
        print('test_MAE={:.3f}, test_RMSE={:.3f}, test_Pearson={:.3f}, test_Spearman={:.3f}, test_R2={:.3f}'.format(*test_error))

        if epoch % opt.save_freq == 0:
            save_file = os.path.join(
                opt.save_folder, 'ckpt_epoch_{epoch}.pth'.format(epoch=epoch))
            save_model(model, optimizer, opt, epoch, save_file)

        #if epoch % opt.save_curr_freq == 0:
        #    save_file = os.path.join(
        #        opt.save_folder, 'curr_last.pth'.format(epoch=epoch))
        #    save_model(model, optimizer, opt, epoch, save_file)

        if is_best:
            best_test = test_error
            torch.save({
                'epoch': epoch,
                'model': model.state_dict(),
                'best_error': best_error
            }, save_file_best)
        print('Best test_MAE={:.3f}, test_RMSE={:.3f}, test_Pearson={:.3f}, test_Spearman={:.3f}, test_R2={:.3f}'.format(*best_test))

    #print("=" * 120)
    #print("Test best model on test set...")
    #checkpoint = torch.load(save_file_best)
    #model.load_state_dict(checkpoint['model'])
    #print(f"Loaded best model, epoch {checkpoint['epoch']}, best val error {checkpoint['best_error']:.3f}")
    #test_error = validate(test_loader, model)
    #print('test_MAE={:.3f}, test_RMSE={:.3f}, test_Pearson={:.3f}, test_Spearman={:.3f}, test_R2={:.3f}'.format(test_error))


if __name__ == '__main__':
    main()
