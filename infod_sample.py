import shutil
import numpy as np
import json
import os
import sys
import time
import math
import io
import torch
import getpass
import torch.nn as nn
import torch.optim as optim
from torchvision import models
import torchvision.datasets as dsets
import torchvision.transforms as transforms
from torchattacks.attack import Attack
from utils import *
from compression import *
from decompression import *
from PIL import ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True


class InfoDrop(Attack):
    r"""    
    Distance Measure : l_inf bound on quantization table
    Arguments:
        model (nn.Module): model to attack.
        steps (int): number of steps. (DEFALUT: 40)
        batch_size (int): batch size
        q_size: bound for quantization table
        targeted: True for targeted attack
    Shape:
        - images: :math:`(N, C, H, W)` where `N = number of batches`, `C = number of channels`,        `H = height` and `W = width`. It must have a range [0, 1].
        - labels: :math:`(N)` where each value :math:`y_i` is :math:`0 \leq y_i \leq` `number of labels`.
        - output: :math:`(N, C, H, W)`. 
        
    """

    def __init__(self, model, height=224, width=224, steps=40, batch_size=20, block_size=8, q_size=10, targeted=False):
        super(InfoDrop, self).__init__("InfoDrop", model)
        self.steps = steps
        self.targeted = targeted
        self.batch_size = batch_size
        self.height = height
        self.width = width
        # Value for quantization range
        self.factor_range = [5, q_size]
        # Differential quantization
        self.alpha_range = [0.1, 1e-20]
        self.alpha = torch.tensor(self.alpha_range[0])
        self.alpha_interval = torch.tensor((self.alpha_range[1] - self.alpha_range[0]) / self.steps)
        block_n = np.ceil(height / block_size) * np.ceil(height / block_size)  # 将图片按block_size=20划分, 一块里有block_n个像素
        q_ini_table = np.empty((batch_size, int(block_n), block_size, block_size),
                               dtype=np.float32)  # shape(20, 784, 8, 8) 20个784维每维度8x8，全部置0
        # q_ini_table.fill(q_size)  # 将q_ini_table填入q_size
        # self.q_tables = {"y": torch.from_numpy(q_ini_table),
        #                  "cb": torch.from_numpy(q_ini_table),
        #                  "cr": torch.from_numpy(q_ini_table)}
        self.q_tables = {}
        marks = ['y', 'cb', 'cr']
        for k in range(0, 3):
            q_ini_table.fill(q_size)
            self.q_tables.update({marks[k]: torch.from_numpy(q_ini_table)})     # 将ndarray转为tensor放入q_tables，否则后续的optimizer无法计算非tensor

    def forward(self, images, labels):
        r"""
        Overridden.
        """
        q_table = None
        self.alpha = self.alpha.to(self.device)
        self.alpha_interval = self.alpha_interval.to(self.device)

        images = images.clone().detach().to(self.device)
        labels = labels.clone().detach().to(self.device)
        adv_loss = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam([self.q_tables["y"], self.q_tables["cb"], self.q_tables["cr"]], lr=0.01)   # learning rate = 0.01

        images = images.permute(0, 2, 3, 1)     # images(D, C, H, W) -> (D, H, W, C)
        components = {'y': images[:, :, :, 0], 'cb': images[:, :, :, 1], 'cr': images[:, :, :, 2]}  # 3 channels of each batch
        for i in range(self.steps):
            self.q_tables["y"].requires_grad = True
            self.q_tables["cb"].requires_grad = True
            self.q_tables["cr"].requires_grad = True
            upresults = {}
            for k in components.keys():
                comp = block_splitting(components[k])   # (20, 224, 224) -> (20, 28*28, 8, 8)，即将每个通道图切成28*28个8x8的块
                comp = dct_8x8(comp)    # execute DCT transform on each 8x8 block
                comp = quantize(comp, self.q_tables[k], self.alpha)
                comp = dequantize(comp, self.q_tables[k])
                comp = idct_8x8(comp)
                merge_comp = block_merging(comp, self.height, self.width)
                upresults[k] = merge_comp

            rgb_images = torch.cat(     # 对张量沿第4维进行拼接
                [upresults['y'].unsqueeze(3), upresults['cb'].unsqueeze(3), upresults['cr'].unsqueeze(3)], dim=3)   # unqueeze(3)去掉最外层的维度
            rgb_images = rgb_images.permute(0, 3, 1, 2)
            outputs = self.model(rgb_images)
            _, pre = torch.max(outputs.data, 1)
            if self.targeted:
                suc_rate = ((pre == labels).sum() / self.batch_size).cpu().detach().numpy()
            else:
                suc_rate = ((pre != labels).sum() / self.batch_size).cpu().detach().numpy()

            adv_cost = adv_loss(outputs, labels)

            if not self.targeted:
                adv_cost = -1 * adv_cost

            total_cost = adv_cost
            optimizer.zero_grad()
            total_cost.backward()

            self.alpha += self.alpha_interval   # alpha是近似floor函数的可微函数参数
            
            # 根据反向传播的结果更新参数
            for k in self.q_tables.keys():
                self.q_tables[k] = self.q_tables[k].detach() - torch.sign(self.q_tables[k].grad)    # formula (7)
                self.q_tables[k] = torch.clamp(self.q_tables[k], self.factor_range[0], self.factor_range[1]).detach()   # torch.clamp(input, min, max): 将输入张量限制到min到max之间
            if i % 10 == 0:
                print('Step: ', i, "  Loss: ", total_cost.item(), "  Current Suc rate: ", suc_rate)
            if suc_rate >= 1:
                print('End at step {} with suc. rate {}'.format(i, suc_rate))
                q_images = torch.clamp(rgb_images, min=0, max=255.0).detach()
                return q_images, pre, i
        q_images = torch.clamp(rgb_images, min=0, max=255.0).detach()

        return q_images, pre, q_table


class Normalize(nn.Module):
    def __init__(self, mean, std):
        super(Normalize, self).__init__()
        self.register_buffer('mean', torch.Tensor(mean))
        self.register_buffer('std', torch.Tensor(std))

    def forward(self, input):
        # Broadcasting
        input = input / 255.0
        mean = self.mean.reshape(1, 3, 1, 1)
        std = self.std.reshape(1, 3, 1, 1)
        return (input - mean) / std     # 将数据点集中在0附近


def save_img(img, img_name, save_dir):
    create_dir(save_dir)
    img_path = os.path.join(save_dir, img_name)
    img_pil = Image.fromarray(img.astype(np.uint8))
    img_pil.save(img_path)

def save_labels():
    pass


def pred_label_and_confidence(model, input_batch, labels_to_class):
    input_batch = input_batch.cuda()
    with torch.no_grad():
        out = model(input_batch)
    _, index = torch.max(out, 1)

    percentage = torch.nn.functional.softmax(out, dim=1) * 100
    # print(percentage.shape)
    pred_list = []
    for i in range(index.shape[0]):
        pred_class = labels_to_class[index[i]]
        pred_conf = str(round(percentage[i][index[i]].item(), 2))
        pred_list.append([pred_class, pred_conf])
    return pred_list


if __name__ == "__main__":
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if getpass.getuser() == 'aaroncomo':
        root_path = '/Users/aaroncomo/coding/python/projects/AdvDrop'
    else:
        root_path = '/wangrun/lwf/AdvDrop'
    class_idx = json.load(open(os.path.join(root_path, 'imagenet_class_index.json')))
    idx2label = [class_idx[str(k)][1] for k in range(len(class_idx))]
    class2label = [class_idx[str(k)][0] for k in range(len(class_idx))]

    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(), ])

    norm_layer = Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])   # 设置normalize layer的参数

    resnet_model = nn.Sequential(   # 设定整个resnet50网络的模型，用于被攻击
        norm_layer,     # normalize layer
        models.resnet50(pretrained=True)    # resnet50网络
    ).to(device)    # 将模型送入设备
    resnet_model = resnet_model.eval()  # evaluation mode

    # Uncomment if you want save results
    try:
        shutil.rmtree(os.path.join(root_path, 'results'))
    except:
        pass
    save_dir = os.path.join(root_path, 'results')
    create_dir(save_dir)

    targeted = True     # targeted攻击, 下面迭代里也要改
    batch_size = 20
    tar_cnt = 1000
    q_size = 40
    cur_cnt = 0
    suc_cnt = 0
    data_dir = os.path.join(root_path, 'test-data')
    data_clean(data_dir)
    # old_data -> new_data没看懂
    normal_data = image_folder_custom_label(root=data_dir, transform=transform, idx2label=class2label)
    normal_loader = torch.utils.data.DataLoader(normal_data, batch_size=batch_size, shuffle=False)

    normal_iter = iter(normal_loader)
    for i in range(0, tar_cnt // batch_size):   # 每次取出一个batch的数据
        print("Iter: ", i)
        images, labels = normal_iter.next()     # 正确的labels

        # For target attack: set random target.
        # Comment if you set untargeted attack.
        original_labels = labels
        labels = torch.from_numpy(np.random.randint(0, 1000, size=batch_size))

        images = images * 255.0
        attack = InfoDrop(resnet_model, batch_size=batch_size, q_size=q_size, steps=150, targeted=targeted)
        at_images, at_labels, suc_step = attack(images, labels)

        # Uncomment following codes if you wang to save the adv imgs
        at_images_np = at_images.detach().cpu().numpy()
        adv_img = at_images_np[0]
        adv_img = np.moveaxis(adv_img, 0, 2)
        adv_dir = os.path.join(save_dir, 'q_size_{}'.format(str(q_size)))
        img_name = "adv_{}.jpg".format(i)
        save_img(adv_img, img_name, adv_dir)
        with open(os.path.join(adv_dir, 'adv_{}.txt'.format(i)), mode='w') as fi:
            if at_labels[0] == labels[0]:
                fi.writelines('Success\n')
            else:
                fi.writelines('Fail\n')
            fi.writelines('original label:\t{}\n'.format(class_idx[str(np.array(original_labels[0]))][1]))
            fi.writelines('target label:\t{}\n'.format(class_idx[str(np.array(labels[0]))][1]))
            fi.writelines('attack label:\t{}\n'.format(class_idx[str(at_labels[0].cpu().numpy())][1]))

        labels = labels.to(device)
        suc_cnt += (at_labels == labels).sum().item()
        print("Current suc. rate: ", suc_cnt / ((i + 1) * batch_size))
    score_list = np.zeros(tar_cnt)
    score_list[:suc_cnt] = 1.0
    stderr_dist = np.std(np.array(score_list)) / np.sqrt(len(score_list))
    print('Avg suc rate: %.5f +/- %.5f' % (suc_cnt / tar_cnt, stderr_dist))
