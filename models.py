__author__ = "Yizhuo Wu, Chang Gao"
__license__ = "MIT License"
__version__ = "1.0"
__email__ = "yizhuo.wu@tudelft.nl, chang.gao@tudelft.nl"

import torch
import torch.nn as nn
from typing import Any, cast, Dict, List, Optional, Union



class CoreModel(nn.Module):
    def __init__(self, hidden_size, num_layers, backbone_type, dim, dt_rank, d_state, image_height, image_width,num_classes, channels, dropout, optional_avg_pool, channel_confusion_layer, channel_confusion_out_channels, time_downsample_factor):
        super(CoreModel, self).__init__()
        self.output_size = 1  
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.backbone_type = backbone_type
        self.batch_first = True  # Force batch first
        self.bidirectional = False
        self.bias = True

        if backbone_type == 'vgg16':
            from torchvision.models import vgg16
            self.backbone = vgg16(pretrained=False)
            # Modify the classifier to match our num_classes
            self.backbone.classifier[-1] = nn.Linear(4096, num_classes)
        elif backbone_type == 'resnet':
            from backbones.ResNet import ResNet
            self.backbone = ResNet(hidden_size=hidden_size,
                                   num_classes=num_classes,
                                   channels=channels,
                                   image_height=image_height,
                                   image_width=image_width)
        elif backbone_type == 'bilstm':
            from backbones.BiLSTM import DopplerBiLSTM
            self.backbone = DopplerBiLSTM(hidden_size=self.hidden_size,
                                          num_classes=num_classes,
                                          channels=channels,
                                          image_height=image_height,
                                          image_width=image_width)
        elif backbone_type == 'cnnlstm':
            from backbones.CnnLstm import CNNLSTM
            self.backbone = CNNLSTM(hidden_size=self.hidden_size,
                                    image_height=image_height,
                                    image_width=image_width,
                                    num_classes=num_classes,
                                    channels=channels)
        elif backbone_type == 'cnngru':
            from backbones.CnnGru import CNNGRU
            self.backbone = CNNGRU(hidden_size=self.hidden_size,
                                    image_height=image_height,
                                    image_width=image_width,
                                    num_classes=num_classes,
                                    channels=channels)
        elif backbone_type == 'radmamba':
            from backbones.RadMamba import RadMamba
            self.backbone = RadMamba(
                dim=dim,  # Dimension of the transformer model
                dt_rank=dt_rank,  # Rank of the dynamic routing matrix
                dim_inner=dim,  # Inner dimension of the transformer model
                d_state=d_state,  # Dimension of the state vector
                num_classes=num_classes,  # Number of output classes
                image_height=image_height,  # Size of the input image
                image_width=image_width,  # Size of the input image
                channels=channels,  # Number of input channels
                dropout=dropout,  # Dropout rate
                depth=1,  # Depth of the transformer model
                channel_confusion_layer=channel_confusion_layer,
                channel_confusion_out_channels=channel_confusion_out_channels,
                time_downsample_factor=time_downsample_factor,
                optional_avg_pool=optional_avg_pool
            )
        elif backbone_type == 'radmamba_modif':
            from backbones.RadMamba_modif import RadMamba as RadMambaMod
            self.backbone = RadMambaMod(
                dim=dim,
                dt_rank=dt_rank,
                dim_inner=dim,
                d_state=d_state,
                num_classes=num_classes,
                image_height=image_height,
                image_width=image_width,
                channels=channels,
                dropout=dropout,
                depth=1,
                channel_confusion_layer=channel_confusion_layer,
                channel_confusion_out_channels=channel_confusion_out_channels,
                time_downsample_factor=time_downsample_factor,
                optional_avg_pool=optional_avg_pool,
                learned_pos=True,
                head_hidden=dim // 2,
                use_stem=True,
                stem_kernel=3,
                drop_path_rate=0.1,
            )
        elif backbone_type == 'radmamba_modif_1':
            from backbones.RadMamba_modif_1 import RadMamba as RadMambaMod1
            self.backbone = RadMambaMod1(
                dim=dim,
                dt_rank=dt_rank,
                dim_inner=dim,
                d_state=d_state,
                num_classes=num_classes,
                image_height=image_height,
                image_width=image_width,
                channels=channels,
                dropout=dropout,
                depth=4,
                channel_confusion_layer=2,
                channel_confusion_out_channels=channel_confusion_out_channels,
                time_downsample_factor=time_downsample_factor,
                optional_avg_pool=optional_avg_pool,
                learned_pos=True,
                head_hidden=dim ,
                use_stem=True,
                stem_kernel=3,
                drop_path_rate=0.1,
                use_se=True,
            )
        elif backbone_type == 'conv_mamba_pyr':
            from backbones.ConvMambaPyramid import ConvMambaPyramid
            self.backbone = ConvMambaPyramid(
                dim=dim,
                dt_rank=dt_rank,
                dim_inner=dim,
                d_state=d_state,
                num_classes=num_classes,
                image_height=image_height,
                image_width=image_width,
                channels=channels,
                dropout=dropout,
                depth=3,
                drop_path_rate=0.1,
            )

        else:
            raise ValueError(f"The backbone type '{self.backbone_type}' is not supported. Please add your own "
                             f"backbone under ./backbones and update models.py accordingly.")

        # Initialize backbone parameters
        try:
            self.backbone.reset_parameters()
            print("Backbone Initialized...")
        except AttributeError:
            pass

    def forward(self, x, h_0=None):
        device = x.device
        batch_size = x.size(0)  # NOTE: dim of x must be (batch, time, feat)/(N, T, F)
        if h_0 is None:  # Create initial hidden states if necessary
            h_0 = torch.zeros(self.num_layers, batch_size, self.hidden_size).to(device)

        # Forward Propagate through the RNN
        out = self.backbone(x)

        return out


class CascadedModel(nn.Module):
    def __init__(self, fextractor_model, classifier_model):
        super(CascadedModel, self).__init__()
        self.fextractor_model = fextractor_model
        self.classifier_model = classifier_model

    def forward(self, x):
        x = self.fextractor_model(x)
        x = self.classifier_model(x)
        return x
