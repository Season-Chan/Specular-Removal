# coding:utf-8
import torch
import cv2 as cv
import numpy as np
from torch import nn
from PIL import Image
import torch.nn.functional as F
from torchvision import transforms as T
import matplotlib as mpl
import matplotlib.pyplot as plt


def exception_handler(predict_func):
    """ 处理图像太大导致的 RuntimeError 异常 """
    def wrapper(model, *args, **kwargs):
        try:
            return predict_func(model, *args, **kwargs)
        except RuntimeError:
            print('😑 图像太大啦，请缩小图像大小后再尝试~~')
            exit()
    return wrapper


class EncoderBlock(nn.Module):
    """ 编码器卷积块 """

    def __init__(self, in_channels: int, out_channel: int, kernel_size=3, padding=0):
        """
        Parameters
        ----------
        in_channels: int
            输入通道数

        out_channels: int
            输出通道数

        kernel_size: int
            卷积核大小

        padding: int
            卷积的 padding 大小
        """
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(in_channels, out_channel, kernel_size, padding=padding),
            nn.BatchNorm2d(out_channel),
            nn.ReLU(),
            nn.MaxPool2d(kernel_size=2)
        )

    def forward(self, x):
        return self.features(x)


class ConvBlock(nn.Module):
    """ 卷积块 """

    def __init__(self, in_channels: int, out_channel: int, kernel_size=3, padding=0, AF=nn.ReLU):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(in_channels, out_channel, kernel_size, padding=padding),
            nn.BatchNorm2d(out_channel),
            AF(),
        )

    def forward(self, x):
        return self.features(x)


class DecoderBlock(nn.Module):
    """ 解码器卷积块 """

    def __init__(self, in_channels: int, out_channel: int, scale_factor: int):
        """
        Parameters
        ----------
        in_channels: int
            输入通道数

        out_channels: int
            输出通道数

        scale_factor: int
            升采样倍数
        """
        super().__init__()
        self.features = nn.Sequential(
            nn.ConvTranspose2d(in_channels, out_channel,
                               scale_factor, scale_factor),
            nn.BatchNorm2d(out_channel),
            nn.ReLU()
        )

    def forward(self, x):
        return self.features(x)


class PartialConvBlock(nn.Module):
    """ 部分卷积模块 """

    def __init__(self, in_channels: int, out_channels: int, kernel_size: int, padding: int, dilation=1):
        super().__init__()
        self.bn = nn.BatchNorm2d(out_channels)
        self.conv_I = nn.Conv2d(in_channels, out_channels,
                                kernel_size, padding=padding, dilation=dilation)
        self.conv_M = nn.Conv2d(
            1, 1, kernel_size, padding=padding, dilation=dilation, bias=False)
        nn.init.constant_(self.conv_M.weight, 1.0)
        self.conv_M.requires_grad_(False)

    def forward(self, x, M):
        """
        Parameters
        ----------
        x: Tensor of shape `(N, C, H, W)`
            特征图

        M: Tensor of shape `(N, 1, H, W)`
            图像蒙版，高光区域的像素值为 `0`
        """
        M = self.conv_M(M)
        index = M == 0
        M[index] = 1
        x = self.conv_I(M*x)
        x = F.relu(self.bn(x/M))
        M = M.masked_fill(index, 0)
        return x, M


class CDFFBlock(nn.Module):
    """ 累计密集特征融合模块 """

    def __init__(self, channels=3):
        """
        Parameters
        ----------
        channels: int
            特征图经过 1 × 1 卷积块之后的通道数
        """
        super().__init__()
        self.channels = 3
        self.conv5 = nn.Conv2d(64, channels, 1)
        self.conv4 = nn.Conv2d(32+channels, channels*2, 1)
        self.conv3 = nn.Conv2d(16+channels*2, channels*3, 1)
        self.conv2 = nn.Conv2d(8+channels*3, channels*4, 1)
        self.conv1 = nn.Conv2d(4+channels*4, channels*5, 1)

    def forward(self, x1, x2, x3, x4, x5):
        x = self.conv5(F.interpolate(x5, scale_factor=2,
                       mode='bilinear', align_corners=True))
        x = torch.cat([x, x4], dim=1)
        x = self.conv4(F.interpolate(x, scale_factor=2,
                       mode='bilinear', align_corners=True))
        x = torch.cat([x, x3], dim=1)
        x = self.conv3(F.interpolate(x, scale_factor=2,
                       mode='bilinear', align_corners=True))
        x = torch.cat([x, x2], dim=1)
        x = self.conv2(F.interpolate(x, scale_factor=2,
                       mode='bilinear', align_corners=True))
        x = torch.cat([x, x1], dim=1)
        x = self.conv1(F.interpolate(x, scale_factor=2,
                       mode='bilinear', align_corners=True))
        return x


class SRNet(nn.Module):
    """ 高光移除网络 """

    def __init__(self):
        super().__init__()
        # 编码器
        self.encoder1 = EncoderBlock(3, 4, 3, padding=1)
        self.encoder2 = EncoderBlock(4, 8, 3, padding=1)
        self.encoder3 = EncoderBlock(8, 16, 3, padding=1)
        self.encoder4 = EncoderBlock(16, 32, 3, padding=1)
        self.encoder5 = EncoderBlock(32, 64, 3, padding=1)
        # 解码器
        self.decoder5 = DecoderBlock(64, 32, scale_factor=2)
        self.decoder4 = DecoderBlock(64, 16, scale_factor=2)
        self.decoder3 = DecoderBlock(32, 8, scale_factor=2)
        self.decoder2 = DecoderBlock(16, 4, scale_factor=2)
        self.decoder1 = DecoderBlock(8, 1, scale_factor=2)
        # CDFF 模块
        self.cdff = CDFFBlock()
        # 输出卷积块
        self.M_conv = nn.Sequential(
            ConvBlock(16, 8, 3, 1),
            ConvBlock(8, 4, 3, 1),
            ConvBlock(4, 1, 3, 1, nn.Sigmoid)
        )
        self.S_conv = nn.Sequential(
            ConvBlock(17, 8, 3, 1),
            ConvBlock(8, 3, 3, 1),
        )
        self.D_conv1 = PartialConvBlock(19, 13, 5, padding=2)
        self.D_conv2 = PartialConvBlock(13, 8, 5, padding=2)
        self.D_conv3 = PartialConvBlock(8, 3, 5, padding=2)

    def forward(self, I):
        """
        Parameters
        ----------
        I: Tensor of shape `(N, C, H, W)`
            有高光的原始图像

        Returns
        -------
        M: Tensor of shape `(N, 1, H, W)`
            高光区域的蒙版

        S: Tensor of shape `(N, 3, H, W)`
            高光区域图像

        D: Tensor of shape `(N, 3, H, W)`
            去掉高光后的图像
        """
        # 编码
        x1 = self.encoder1(I)
        x2 = self.encoder2(x1)
        x3 = self.encoder3(x2)
        x4 = self.encoder4(x3)
        x5 = self.encoder5(x4)
        x_cdff = self.cdff(x1, x2, x3, x4, x5)
        # 解码
        x6 = torch.cat([self.decoder5(x5), x4], dim=1)
        x7 = torch.cat([self.decoder4(x6), x3], dim=1)
        x8 = torch.cat([self.decoder3(x7), x2], dim=1)
        x9 = torch.cat([self.decoder2(x8), x1], dim=1)
        x10 = torch.cat([self.decoder1(x9), x_cdff], dim=1)
        M = self.M_conv(x10)
        S = self.S_conv(torch.cat([x10, M], dim=1))
        D, M_ = self.D_conv1(torch.cat([x10, I-M*S], dim=1), 1-M)
        D, M_ = self.D_conv2(D, M_)
        D, M_ = self.D_conv3(D, M_)
        return M, S, D

    @exception_handler
    def predict(self, image: Image.Image, use_gpu=True):
        """ 预测高光区域的蒙版、高光区域图像和去掉高光后的图像

        Parameters
        ----------
        image: ~PIL.Image.Image
            PIL 图像

        use_gpu: bool
            是否使用 GPU

        Return
        ------
        M: ~PIL.Image.Image
            去掉高光后的图像

        S: ~PIL.Image.Image
            高光图像

        D: ~PIL.Image.Image
            去掉高光后的图像
        """
        if image.mode != 'RGB':
            image = image.convert('RGB')

        # 将图像填充到宽高都是 32 的整数倍
        w, h = image.size
        w_padded = (w//32+(w % 32 != 0))*32
        h_padded = (h//32+(h % 32 != 0))*32
        image_padded = cv.copyMakeBorder(
            np.uint8(image), 0, h_padded-h, 0, w_padded-w, cv.BORDER_REFLECT)

        # 预测
        image = T.ToTensor()(image_padded).unsqueeze(0)
        M, S, D = self(image.to('cuda:0' if use_gpu else 'cpu'))
        M = T.ToPILImage()(M.to('cpu').ge(0.5).to(torch.float32).squeeze())
        S = T.ToPILImage()(S.to('cpu').squeeze())
        D = T.ToPILImage()(D.to('cpu').squeeze())

        M = M.crop((0, 0, w, h))
        S = S.crop((0, 0, w, h))
        D = D.crop((0, 0, w, h))
        return M, S, D

    def remove_specular(self, image: Image.Image):
        """ 获取去掉高光后的图像

        Parameters
        ----------
        image: ~PIL.Image.Image
            PIL 图像
        """
        return self.predict(image)[-1]


if __name__ == '__main__':
    image = Image.open('../resource/images/塑料盒.png')
    model = SRNet().to('cuda:0')
    M, S, D = model.predict(image)

    mpl.rc_file('../resource/style/image_process.mplstyle')

    fig, axes = plt.subplots(1, 4, num='高光去除')
    images = [image, M, S, D]
    titles = ['Original image', 'Specular mask',
              'Specular image', 'Specular removal image']
    for ax, im, title in zip(axes, images, titles):
        cmap = plt.cm.gray if title == 'Specular mask' else None
        ax.imshow(im, cmap=cmap)
        ax.set_title(title)

    plt.show()
