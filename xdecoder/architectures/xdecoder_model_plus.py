# --------------------------------------------------------
# X-Decoder -- Generalized Decoding for Pixel, Image, and Language
# Copyright (c) 2022 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Xueyan Zou (xueyan@cs.wisc.edu), Ziyi Dou, Jianwei Yang
# --------------------------------------------------------

from typing import Tuple
import random

import torch
from torch import nn
from torch.nn import functional as F
import numpy as np

from timm.models.layers import trunc_normal_
from nltk.stem.lancaster import LancasterStemmer
from detectron2.structures import Boxes, ImageList, Instances, BitMasks, BoxMode
from detectron2.utils.memory import retry_if_cuda_oom
from detectron2.data import MetadataCatalog

from .registry import register_model
from ..utils import configurable, get_class_names
from ..backbone import build_backbone, Backbone
from ..body import build_xdecoder_head
from ..modules import sem_seg_postprocess, SetCriterion, HungarianMatcher, bbox_postprocess
from ..language import build_language_encoder
from ..language.loss import vl_similarity, image_text_contrastive_loss_queue
from ..language.misc import vl_similarity
from utils.prompt_engineering import prompt_engineering
from utils.constants import COCO_PANOPTIC_CLASSES
from xdecoder.utils import box_ops

st = LancasterStemmer()


class GeneralizedXdecoderPlus(nn.Module):

    @configurable
    def __init__(
        self,
        *,
        backbone: Backbone,
        sem_seg_head: nn.Module,
        criterion: nn.Module,
        losses: dict,
        num_queries: int,
        object_mask_threshold: float,
        overlap_threshold: float,
        metadata,
        task_switch: dict,
        phrase_prob: float,
        size_divisibility: int,
        sem_seg_postprocess_before_inference: bool,
        pixel_mean: Tuple[float],
        pixel_std: Tuple[float],
        # inference
        semantic_on: bool,
        panoptic_on: bool,
        instance_on: bool,
        test_topk_per_image: int,
        train_dataset_name: str,
        retrieval_emsemble: bool,
        backbone_dim: int,
        dim_proj: int,
    ):
        """
        Args:
            backbone: a backbone module, must follow detectron2's backbone interface
            sem_seg_head: a module that predicts semantic segmentation from backbone features
            criterion: a module that defines the loss
            num_queries: int, number of queries
            object_mask_threshold: float, threshold to filter query based on classification score
                for panoptic segmentation inference   论文里没提这个
            overlap_threshold: overlap threshold used in general inference for panoptic segmentation
            metadata: dataset meta, get `thing` and `stuff` category names for panoptic
                segmentation inference
            size_divisibility: Some backbones require the input height and width to be divisible by a
                specific integer. We can use this to override such requirement.
            sem_seg_postprocess_before_inference: whether to resize the prediction back
                to original input size before semantic segmentation inference or after.
                For high-resolution dataset like Mapillary, resizing predictions before
                inference will cause OOM error.
            pixel_mean, pixel_std: list or tuple with #channels element, representing
                the per-channel mean and std to be used to normalize the input image
            semantic_on: bool, whether to output semantic segmentation prediction
            instance_on: bool, whether to output instance segmentation prediction
            panoptic_on: bool, whether to output panoptic segmentation prediction
            test_topk_per_image: int, instance segmentation parameter, keep topk instances per image
        """
        super().__init__()
        self.backbone = backbone
        self.sem_seg_head = sem_seg_head
        self.criterion = criterion
        self.losses = losses
        self.num_queries = num_queries
        self.overlap_threshold = overlap_threshold
        self.object_mask_threshold = object_mask_threshold
        self.metadata = metadata
        if size_divisibility < 0:
            # use backbone size_divisibility if not set
            size_divisibility = self.backbone.size_divisibility
        self.size_divisibility = size_divisibility
        self.sem_seg_postprocess_before_inference = sem_seg_postprocess_before_inference
        self.register_buffer("pixel_mean", torch.Tensor(pixel_mean).view(-1, 1, 1), False)
        self.register_buffer("pixel_std", torch.Tensor(pixel_std).view(-1, 1, 1), False)

        # additional args
        self.semantic_on = semantic_on
        self.instance_on = instance_on
        self.panoptic_on = panoptic_on
        self.detection_on = detection_on

        #detection args


        # caption argument
        self.task_switch = task_switch
        self.phrase_prob = phrase_prob

        self.test_topk_per_image = test_topk_per_image
        # TODO train_class_name 变成字典

        self.train_class_names = dict()
        self.train_dataset_name = train_dataset_name
        self.coco_mask_on = coco_mask_on
        self.task_switch = {'coco': coco_on, 'o365': o365_on}
        print("self.task_switch ", self.task_switch)
        # HACK for only two datasets for seg and det
        if coco_on:
            task = 'seg'
            if not coco_mask_on:
                task = 'det'
            self.train_class_names[task] = get_class_names(train_dataset_name[0], background=background)
            self.train_class_names[task] = [a.replace("-merged", "").replace("-other", "").replace("-stuff", "") for a
                                            in self.train_class_names[task]]
            train_class_names = []
            for name in self.train_class_names[task]:
                names = name.split('-')
                if len(names) > 1:
                    assert len(names) == 2
                    train_class_names.append(names[1] + ' ' + names[0])
                else:
                    train_class_names.append(name)
            self.train_class_names[task] = train_class_names

        if o365_on and len(train_dataset_name) > 1:
            self.train_class_names['det'] = get_class_names(train_dataset_name[1], background=background)
            self.train_class_names['det'] = [a.lower().split('/') for a in self.train_class_names['det']]


        self.retrieval_emsemble = retrieval_emsemble
        # backbone itc loss
        if task_switch['retrieval'] and retrieval_emsemble:
            self.backbone_proj = nn.Parameter(torch.empty(backbone_dim, dim_proj))
            trunc_normal_(self.backbone_proj, std=.02)


        if not self.semantic_on:
            assert self.sem_seg_postprocess_before_inference

    @classmethod
    def from_config(cls, cfg):
        enc_cfg = cfg['MODEL']['ENCODER']
        dec_cfg = cfg['MODEL']['DECODER']

        # Loss parameters:
        deep_supervision = dec_cfg['DEEP_SUPERVISION']
        no_object_weight = dec_cfg['NO_OBJECT_WEIGHT']

        # loss weights, switcher for task, and top layers to compute loss
        loss_weights = {'mask': {'ce': dec_cfg['CLASS_WEIGHT'], 'dice': dec_cfg['DICE_WEIGHT'], 'bce': dec_cfg['MASK_WEIGHT']},
                        'bbox': {'l1': dec_cfg['BBOX_WEIGHT'], 'giou': dec_cfg['GIOU_WEIGHT']}, # TODO: 这个loss可以用于detection 任务么？
                        'caption': dec_cfg['CAPTION_WEIGHT'],
                        'captioning': dec_cfg['CAPTIONING_WEIGHT'], 
                        'retrieval': {'decoder': dec_cfg['RETRIEVAL_WEIGHT'], 'backbone': dec_cfg['BACKBONER_WEIGHT']},
                        'grounding': {'ce': dec_cfg['GCLASS_WEIGHT'], 'dice': dec_cfg['GDICE_WEIGHT'], 'bce': dec_cfg['GMASK_WEIGHT']}}
                        #TODO: 单独列出需要匈牙利匹配的loss权重

        task_switch = {'bbox': dec_cfg.get('DETECTION', False),
                       'mask': dec_cfg.get('MASK', True),
                       'caption': dec_cfg['CAPTION'].get('ENABLED', False),
                       'captioning': dec_cfg['CAPTIONING'].get('ENABLED', False),
                       'retrieval': dec_cfg['RETRIEVAL'].get('ENABLED', False),
                       'grounding': dec_cfg['GROUNDING'].get('ENABLED', False)}

        top_x_layers = {'mask': dec_cfg.get('TOP_MASK_LAYERS', 10),
                        'caption': dec_cfg.get('TOP_CAPTION_LAYERS', 10), 
                        'captioning': dec_cfg.get('TOP_CAPTIONING_LAYERS', 10),
                        'retrieval': dec_cfg.get('TOP_RETRIEVAL_LAYERS', 10),
                        'grounding': dec_cfg.get('TOP_GROUNDING_LAYERS', 10),
                        'bbox': dec_cfg.get('TOP_DETECTION_LAYERS', 10)
                        }

        # build model
        extra = {'task_switch': task_switch}
        backbone = build_backbone(cfg)
        lang_encoder = build_language_encoder(cfg)        
        sem_seg_head = build_xdecoder_head(cfg, backbone.output_shape(), lang_encoder, extra)

        # building criterion
        matcher = HungarianMatcher(   # 这个类非常重要
            cost_class=loss_weights['mask']['ce'],
            cost_mask=loss_weights['mask']['bce'],
            cost_dice=loss_weights['mask']['dice'],
            num_points=dec_cfg['TRAIN_NUM_POINTS'],
            # Box critercion
            # panoptic_on: bool = False
            cost_box = loss_weights['bbox']['l1'],
            cost_giou = loss_weights['bbox']['giou'],
        )

        # init weight dict and criterion loss functions.
        losses = {'seg': [], 'vlp': [], 'bbox':[]}
        if task_switch['mask']:
            losses['seg'] += ["labels", "masks"]
        if task_switch['caption']:
            losses['seg'] += ["captions"]
        if task_switch['grounding']:
            losses['seg'] += ["groundings"]
        if task_switch['captioning']:
            losses['vlp'] += ["captionings"]
        if task_switch['retrieval']:
            losses['vlp'] += ["retrievals"]
        if task_switch['bbox']:
            losses['bbox'] +=  ["labels", "boxes"] # O365没有mask

        weight_dict = {}
        for key, turn_on in task_switch.items():
            if turn_on:
                if isinstance(loss_weights[key], dict):
                    # HACK it should support bbox in the future
                    for key_, weight in loss_weights[key].items():
                        weight_dict["loss_{}_{}_0".format(key, key_)] = weight # NOTE: hard code for segmentation that has multiple loss
                else:
                    weight_dict["loss_{}_0".format(key)] = loss_weights[key]
        
        # generate full weight dict and remove not computed layers. 
        if deep_supervision:  # 在模型的中间层添加辅助分类器(Auxiliary Classifier),并构建对应的损失函数,对中间特征进行监督。
            dec_layers = dec_cfg['DEC_LAYERS']
            aux_weight_dict = {}
            for i in range(dec_layers - 1):
                for k, v in weight_dict.items():
                    if (i+1) > (top_x_layers[k.split('_')[1]] - 1):
                        continue
                    aux_weight_dict.update({k.replace('_0', f"_{i+1}"): v})
            weight_dict.update(aux_weight_dict)

        grd_weight = {'text': dec_cfg['GROUNDING']['TEXT_WEIGHT'], 'class': dec_cfg['GROUNDING']['CLASS_WEIGHT']}
        # generate critenrion for loss function.
        criterion = SetCriterion(
            sem_seg_head.num_classes,
            matcher=matcher,
            weight_dict=weight_dict,
            top_x_layers=top_x_layers,
            eos_coef=no_object_weight,
            losses=[],
            num_points=dec_cfg['TRAIN_NUM_POINTS'],
            oversample_ratio=dec_cfg['OVERSAMPLE_RATIO'],
            importance_sample_ratio=dec_cfg['IMPORTANCE_SAMPLE_RATIO'],
            grounding_weight=grd_weight,
        )

        # extra logistic
        train_dataset_name = cfg['DATASETS']['TRAIN'][0] # HACK for only one training set.
        phrase_prob = dec_cfg['CAPTION'].get('PHRASE_PROB', 0.5)

        return {
            "backbone": backbone,
            "sem_seg_head": sem_seg_head,
            "criterion": criterion,
            "losses": losses,
            "num_queries": dec_cfg['NUM_OBJECT_QUERIES'],
            "object_mask_threshold": dec_cfg['TEST']['OBJECT_MASK_THRESHOLD'],
            "overlap_threshold": dec_cfg['TEST']['OVERLAP_THRESHOLD'],
            "metadata": MetadataCatalog.get(cfg['DATASETS']['TRAIN'][0]),
            "size_divisibility": dec_cfg['SIZE_DIVISIBILITY'],
            "sem_seg_postprocess_before_inference": (
                dec_cfg['TEST']['SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE']
                or dec_cfg['TEST']['PANOPTIC_ON']
                or dec_cfg['TEST']['INSTANCE_ON']
            ),
            "pixel_mean": cfg['INPUT']['PIXEL_MEAN'],
            "pixel_std": cfg['INPUT']['PIXEL_STD'],
            "task_switch": task_switch,
            "phrase_prob": phrase_prob,
            # inference
            "semantic_on": dec_cfg['TEST']['SEMANTIC_ON'],
            "instance_on": dec_cfg['TEST']['INSTANCE_ON'],
            "panoptic_on": dec_cfg['TEST']['PANOPTIC_ON'],
            "test_topk_per_image": cfg['COCO']['TEST']['DETECTIONS_PER_IMAGE'],
            "train_dataset_name": train_dataset_name,
            "retrieval_emsemble": dec_cfg['RETRIEVAL']['ENSEMBLE'],
            "backbone_dim": cfg['MODEL']['BACKBONE_DIM'],
            "dim_proj": cfg['MODEL']['DIM_PROJ'],
        }

    @property
    def device(self):
        return self.pixel_mean.device

    def forward(self, batched_inputs, mode=None):
        """
        Args:
            batched_inputs: a list, batched outputs of :class:`DatasetMapper`.
                Each item in the list contains the inputs for one image.
                For now, each item in the list is a dict that contains:
                   * "image": Tensor, image in (C, H, W) format.
                   * "instances": per-region ground truth
                   * Other information that's included in the original dicts, such as:
                     "height", "width" (int): the output resolution of the model (may be different
                     from input resolution), used in inference.
                     # TODO: box ground truth
        Returns:
            list[dict]:
                each dict has the results for one image. The dict contains the following keys:

                * "sem_seg":
                    A Tensor that represents the
                    per-pixel segmentation prediced by the head.
                    The prediction has shape KxHxW that represents the logits of
                    each class for each pixel.
                * "panoptic_seg":
                    A tuple that represent panoptic output
                    panoptic_seg (Tensor): of shape (height, width) where the values are ids for each segment.
                    segments_info (list[dict]): Describe each segment in `panoptic_seg`.
                        Each dict contains keys "id", "category_id", "isthing".
                # TODO: Box prediction
        """
        if self.training: #对应多种任务
            losses = {}
            if self.task_switch['mask']:
                losses_seg = self.forward_seg(batched_inputs['coco'])
                losses.update(losses_seg)

            if self.task_switch['retrieval'] or self.task_switch['captioning']:
                losses_vlp = self.forward_vlp(batched_inputs['vlp'])
                losses.update(losses_vlp)

            # TODO: task_switch['box'] forward_box  batch_inputs['object365']?
            if self.task_switch['bbox']:
                self.criterion.num_classes = 365
                losses_o365 = self.forward_seg(batched_inputs['o365'], task='det')
                new_losses_o365 = {}
                for key, value in losses_o365.items():
                    new_losses_o365['o365.' + str(key)] = losses_o365[key]
                losses.update(new_losses_o365)

            for k in list(losses.keys()):
                if k in self.criterion.weight_dict:
                    losses[k] *= self.criterion.weight_dict[k]  # TODO: 添加Object365的weight_dict
                else: # remove this loss if not specified in `weight_dict`
                    losses.pop(k)
            return losses
        else:
            if mode == 'retrieval':
                return self.evaluate_retrieval(batched_inputs)
            elif mode == 'captioning':
                return self.evaluate_captioning(batched_inputs)
            elif mode == 'classification':
                return self.evaluate_classification(batched_inputs)
            elif mode == 'grounding_refcoco':
                return self.evaluate_grounding(batched_inputs, mode)
            else:
                return self.evaluate(batched_inputs)

        
    def forward_seg(self, batched_inputs, task = 'seg'):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)

        if task == "det" and self.task_switch['o365']:
            train_class_names = [random.sample(name, 1)[0] for name in self.train_class_names['det']]
        else:
            train_class_names = self.train_class_names[task]

        self.sem_seg_head.predictor.lang_encoder.get_text_embeddings(train_class_names, is_eval=False)

        extra = {}
        # mask classification target
        if "instances" in batched_inputs[0]:
            # input bounding box is checked to be correct.
            targets = self.prepare_targets(batched_inputs, images, task)

            if self.task_switch['grounding']:
                grounding_tokens = [x['grounding_query_embs'] for x in targets] # need to pad for more than one grounding token
                grounding_tokens = nn.utils.rnn.pad_sequence(grounding_tokens)
                extra['grounding_tokens'] = grounding_tokens

        features = self.backbone(images.tensor)
        outputs = self.sem_seg_head(features, task, extra=extra)

        _outputs = {}
        for key, value in outputs.items():
            if key == 'pred_logits':
                _outputs[key] = value[:,:self.num_queries-1]
            elif key == 'pred_masks':
                _outputs[key] = value[:,:self.num_queries-1]
                if self.task_switch['grounding']:
                    _outputs['pred_gmasks'] = value[:,self.num_queries:2*self.num_queries-1]
            elif key == 'pred_captions':
                _outputs[key] = value[:,:self.num_queries-1]
                if self.task_switch['grounding']:
                    _outputs['pred_gtexts'] = value[:,self.num_queries:2*self.num_queries-1]
            elif key == 'pred_boxes':
                _outputs[key] = value[:, :self.num_queries - 1]

            elif key == 'aux_outputs':
                _outputs[key] = []
                for i in range(len(value)):
                    _outputs[key] += [{}]
                    for _key, _value in value[i].items():
                        if _key == 'pred_logits':
                            _outputs[key][i][_key] = _value[:,:self.num_queries-1]
                        elif _key == 'pred_masks':
                            _outputs[key][i][_key] = _value[:,:self.num_queries-1]
                            if self.task_switch['grounding']:
                                _outputs[key][i]['pred_gmasks'] = _value[:,self.num_queries:2*self.num_queries-1]
                        elif _key == 'pred_captions':
                            _outputs[key][i][_key] = _value[:,:self.num_queries-1]
                            if self.task_switch['grounding']:
                                _outputs[key][i]['pred_gtexts'] = _value[:,self.num_queries:2*self.num_queries-1]        
        outputs = _outputs

        extra = {'lang_logit': self.sem_seg_head.predictor.lang_encoder.logit_scale,
                 'class_embeddings': getattr(self.sem_seg_head.predictor.lang_encoder, '{}_text_embeddings'.format('default'))}

        # bipartite matching-based loss
        if task == 'seg':
            self.criterion.losses = self.losses['seg'] # seg criterion losses
            losses = self.criterion(outputs, targets, extra)
        elif task == 'det':
            self.criterion.losses = self.losses['bbox']  # seg criterion losses
            losses = self.criterion(outputs, targets, extra)

        del outputs
        del _outputs
        return losses

    def forward_vlp(self, batched_inputs):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)
        targets_vlp = self.prepare_vlp_targets(batched_inputs, images.tensor.device)

        extra = {"token_embedding": self.sem_seg_head.predictor.lang_encoder.lang_encoder.token_embedding,
                 "lang_encoder": self.sem_seg_head.predictor.lang_encoder,
                 "training": self.training}

        features = self.backbone(images.tensor)
        outputs = self.sem_seg_head(features, target_queries=None, target_vlp=targets_vlp, task='vlp', extra=extra)

        for key, value in outputs.items():
            if key == 'pred_captionings':
                outputs[key] = value
            elif key == 'pred_captions':
                # outputs[key] = value[:,-1:]
                outputs[key] = value
            elif key == 'aux_outputs':
                outputs[key] = []
                for i in range(len(value)):
                    outputs[key] += [{}]
                    for _key, _value in value[i].items():
                        if _key == 'pred_captions':
                            # outputs[key][i][_key] = _value[:,-1:]
                            outputs[key][i][_key] = _value
                        elif _key == 'pred_captionings':
                            outputs[key][i][_key] = _value

        self.criterion.losses = self.losses['vlp'] # seg criterion losses
        losses = self.criterion.forward_vlp(outputs, targets_vlp, extra)
        del outputs

        if self.task_switch['retrieval'] and self.retrieval_emsemble:
            # compute backbone vlp.
            v_emb = features['res5']
            bs,nc,_,_ = v_emb.shape
            v_emb = v_emb.reshape(bs,nc,-1)
            v_emb = F.adaptive_avg_pool1d(v_emb, 1).reshape(bs,nc) @ self.backbone_proj
            t_emb = torch.cat([x['caption_proj'] for x in targets_vlp], dim=0)
            loss_contrast = image_text_contrastive_loss_queue(v_emb, t_emb, self.sem_seg_head.predictor.lang_encoder, None)
            losses['loss_retrieval_backbone_0'] = loss_contrast
        return losses

    def evaluate(self, batched_inputs):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        
        images = ImageList.from_tensors(images, self.size_divisibility)
        img_bs = images.tensor.shape[0]

        targets = targets_grounding = queries_grounding = None
        features = self.backbone(images.tensor)
        outputs = self.sem_seg_head(features, target_queries=queries_grounding)

        mask_cls_results = outputs["pred_logits"]
        mask_pred_results = outputs["pred_masks"]
        box_pred_results = outputs["pred_boxes"] if self.task_switch['bbox'] else [None for i in range(len(mask_pred_results))]
        caption_pred_results = outputs["pred_captions"] if self.task_switch['caption'] else [None for i in range(len(mask_pred_results))]

        # upsample masks
        mask_pred_results = F.interpolate(
            mask_pred_results,
            size=(images.tensor.shape[-2], images.tensor.shape[-1]),
            mode="bicubic",
            align_corners=False,
            antialias=True
        )

        input_size = mask_pred_results.shape[-2:]
        keep_sem_bgd = self.metadata.keep_sem_bgd if hasattr(self.metadata, 'keep_sem_bgd') else False
        del outputs

        processed_results = []
        for mask_cls_result, mask_pred_result, box_pred_result, caption_pred_result, input_per_image, image_size in zip(
            mask_cls_results, mask_pred_results, box_pred_results, caption_pred_results, batched_inputs, images.image_sizes
        ):
            height = input_per_image.get("height", image_size[0])
            width = input_per_image.get("width", image_size[1])
            processed_results.append({})

            if self.sem_seg_postprocess_before_inference:
                mask_pred_result = retry_if_cuda_oom(sem_seg_postprocess)(
                    mask_pred_result, image_size, height, width
                )
                mask_cls_result = mask_cls_result.to(mask_pred_result)

            # semantic segmentation inference
            if self.semantic_on:
                r = retry_if_cuda_oom(self.semantic_inference)(mask_cls_result, mask_pred_result, keep_sem_bgd)
                if not self.sem_seg_postprocess_before_inference:
                    r = retry_if_cuda_oom(sem_seg_postprocess)(r, image_size, height, width)
                processed_results[-1]["sem_seg"] = r

            # panoptic segmentation inference
            if self.panoptic_on:
                panoptic_r = retry_if_cuda_oom(self.panoptic_inference)(mask_cls_result, mask_pred_result)
                processed_results[-1]["panoptic_seg"] = panoptic_r
            
            # instance segmentation inference
            if self.instance_on:
                if self.task_switch['bbox']:
                    box_pred_result = bbox_postprocess(box_pred_result, input_size, image_size, height, width)
                instance_r = retry_if_cuda_oom(self.instance_inference)(mask_cls_result, mask_pred_result, box_pred_result)
                processed_results[-1]["instances"] = instance_r
            if self.task_switch['caption']:
                processed_results[-1]["captions"] = caption_pred_result
                processed_results[-1]["masks"] = mask_pred_result

        return processed_results

    def evaluate_retrieval(self, batched_inputs):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)
        img_bs = images.tensor.shape[0]
        
        targets = targets_grounding = queries_grounding = None
        features = self.backbone(images.tensor)
        outputs = self.sem_seg_head(features, target_queries=queries_grounding)
        v_emb_it = outputs['pred_captions'][:,-1]

        # compute backbone score
        if self.task_switch['retrieval'] and self.retrieval_emsemble:
            _v_emb_it = features['res5']
            bs,nc,_,_ = _v_emb_it.shape
            _v_emb_it = _v_emb_it.reshape(bs,nc,-1)
            _v_emb_it = F.adaptive_avg_pool1d(_v_emb_it, 1).reshape(bs,nc) @ self.backbone_proj

        processed_results = []
        for idx, batch_data in enumerate(batched_inputs):
            caption_ids = []
            t_emb_its = []
            processed_results.append({})
            for caption in batch_data['captions']:
                lang_results = self.sem_seg_head.predictor.lang_encoder.get_text_token_embeddings(caption)
                t_emb_it = lang_results['class_emb']
                caption_ids.append(batch_data['image_id'])
                t_emb_its.append(t_emb_it)

            t_emb_it = torch.cat(t_emb_its, dim=0)

            image_embeds = [v_emb_it[idx].unsqueeze(0)]
            if self.task_switch['retrieval'] and self.retrieval_emsemble:
                image_embeds += [_v_emb_it[idx].unsqueeze(0)]
            caption_results = {
                    'image_embeds': image_embeds,
                    'text_embeds': t_emb_it,
                    'caption_ids': caption_ids,
                    'image_ids': batch_data['image_id'],
                }
            processed_results[-1]["caption"] = caption_results            
        return processed_results

    def evaluate_captioning(self, batched_inputs):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)
        img_bs = images.tensor.shape[0]

        if not hasattr(self, 'start_token'):
            self.start_token = torch.tensor([[49406]*77], device=self.device)
        
        targets = targets_grounding = queries_grounding = None
        features = self.backbone(images.tensor)

        captioning_mask = None
        if 'captioning_mask' in batched_inputs[-1]:
            captioning_mask = torch.cat([x['captioning_mask'] for x in batched_inputs])

        outputs = self.sem_seg_head(features, target_queries=queries_grounding, task='captioning_infer', extra={'start_token': self.start_token, 'captioning_mask': captioning_mask})

        processed_results = []
        for idx, batch_data in enumerate(batched_inputs):
            processed_results.append({})
            processed_results[-1]["captioning_token"] = outputs['pred_captionings'][idx]
            processed_results[-1]["captioning_text"] = outputs['pred_texts'][idx].split('.')[0]
            processed_results[-1]["image_id"] = batched_inputs[idx]['image_id']
            
        return processed_results

    def evaluate_classification(self, batched_inputs):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)
        img_bs = images.tensor.shape[0]
        
        targets = targets_grounding = queries_grounding = None
        features = self.backbone(images.tensor)
        outputs = self.sem_seg_head(features, target_queries=queries_grounding)

        processed_results = []
        for idx, batch_data in enumerate(batched_inputs):
            processed_results.append({})
            processed_results[-1]["pred_class"] = outputs['pred_logits'][idx,-1]
        return processed_results

    def evaluate_grounding_baseline(self, batched_inputs, mode):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)
        img_bs = images.tensor.shape[0]
        
        targets = targets_grounding = queries_grounding = None
        features = self.backbone(images.tensor)
        outputs = self.sem_seg_head(features, target_queries=queries_grounding)

        mask_pred_results = outputs["pred_masks"]
        caption_pred_results = outputs["pred_captions"] if self.task_switch['caption'] else [None for i in range(len(mask_pred_results))]

        # upsample masks
        mask_pred_results = F.interpolate(
            mask_pred_results,
            size=(images.tensor.shape[-2], images.tensor.shape[-1]),
            mode="bicubic",
            align_corners=False,
            antialias=True
        )

        processed_results = []
        for mask_pred_result, caption_pred_result, input_per_image, image_size in zip(
            mask_pred_results, caption_pred_results, batched_inputs, images.image_sizes
        ):
            height = input_per_image.get("height", image_size[0])
            width = input_per_image.get("width", image_size[1])
            processed_results.append({})

            mask_pred_result = retry_if_cuda_oom(sem_seg_postprocess)(
                mask_pred_result, image_size, height, width
            )[:-1]

            texts_all = input_per_image['groundings']['texts']
            grd_masks = []
            for texts in texts_all:
                if mode == 'grounding_refcoco':
                    self.sem_seg_head.predictor.lang_encoder.get_text_embeddings(texts, name='grounding', prompt=False, is_eval=True)
                elif mode == 'grounding_phrasecut':
                    self.sem_seg_head.predictor.lang_encoder.get_text_embeddings(texts, name='grounding', prompt=True, is_eval=False)
                t_emb = getattr(self.sem_seg_head.predictor.lang_encoder, "{}_text_embeddings".format('grounding')).t()
                v_emb = caption_pred_result[:-1]
                v_emb = v_emb / (v_emb.norm(dim=-1, keepdim=True) + 1e-7)
                vt_sim = v_emb @ t_emb
                max_id = vt_sim.max(0)[1][0]
                grd_masks += [mask_pred_result[max_id]]
            processed_results[-1]['grounding_mask'] = torch.stack(grd_masks)

        return processed_results

    def evaluate_grounding(self, batched_inputs, mode):
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)

        extra = {}
        # mask_pred_results = []
        # for idx, batch_per_image in enumerate(batched_inputs):
        #     grd_texts = batch_per_image['groundings']['texts']
        #     grd_masks = []
        #     for anno_text in grd_texts:
        #         gtext = self.sem_seg_head.predictor.lang_encoder.get_text_token_embeddings([anno_text[0]], name='grounding', token=False, norm=False)
        #         token_emb = gtext['token_emb']
        #         tokens = gtext['tokens']
            
        #         grd_emb = token_emb[0][tokens['attention_mask'].bool()[0]]
        #         extra['grounding_tokens'] = grd_emb[:,None]

        #         assert len(images.tensor) == 1, "grounding evaluation only support single batch size now"
        #         features = self.backbone(images.tensor)
        #         outputs = self.sem_seg_head(features, extra=extra, task='grounding_eval')
                
        #         pred_gmasks = outputs['pred_masks'][idx,self.num_queries:2*self.num_queries-1]
        #         v_emb = outputs['pred_captions'][idx,self.num_queries:2*self.num_queries-1]
        #         t_emb = grd_emb[-1:]

        #         t_emb = t_emb / (t_emb.norm(dim=-1, keepdim=True) + 1e-7)
        #         v_emb = v_emb / (v_emb.norm(dim=-1, keepdim=True) + 1e-7)            

        #         temperature = self.sem_seg_head.predictor.lang_encoder.logit_scale
        #         out_prob = vl_similarity(v_emb, t_emb, temperature=temperature)
                
        #         matched_id = out_prob.max(0)[1]
        #         grd_masks += [pred_gmasks[matched_id,:,:]]
        #     mask_pred_results += [torch.cat(grd_masks)]

        # comment for multi object inference.
        mask_pred_results = []
        for idx, batch_per_image in enumerate(batched_inputs):
            grd_texts = batch_per_image['groundings']['texts']
            grd_texts = [x[0] for x in grd_texts]

            gtext = self.sem_seg_head.predictor.lang_encoder.get_text_token_embeddings(grd_texts, name='grounding', token=False, norm=False)
            token_emb = gtext['token_emb']
            tokens = gtext['tokens']
            query_emb = token_emb[tokens['attention_mask'].bool()]
            extra['grounding_tokens'] = query_emb[:,None]

            features = self.backbone(images.tensor)
            outputs = self.sem_seg_head(features, extra=extra, task='grounding_eval')

            pred_gmasks = outputs['pred_masks'][idx,self.num_queries:2*self.num_queries-1]
            v_emb = outputs['pred_captions'][idx,self.num_queries:2*self.num_queries-1]
            t_emb = gtext['class_emb']

            t_emb = t_emb / (t_emb.norm(dim=-1, keepdim=True) + 1e-7)
            v_emb = v_emb / (v_emb.norm(dim=-1, keepdim=True) + 1e-7)            

            temperature = self.sem_seg_head.predictor.lang_encoder.logit_scale
            out_prob = vl_similarity(v_emb, t_emb, temperature=temperature)
            
            matched_id = out_prob.max(0)[1]
            mask_pred_results += [pred_gmasks[matched_id,:,:]]

        for i in range(len(mask_pred_results)):
            # upsample masks
            mask_pred_results[i] = F.interpolate(
                mask_pred_results[i][None,],
                size=(images.tensor.shape[-2], images.tensor.shape[-1]),
                mode="bicubic",
                align_corners=False,
                antialias=True
            )[0]

        processed_results = []
        for mask_pred_result, input_per_image, image_size in zip(
            mask_pred_results, batched_inputs, images.image_sizes
        ):
            height = input_per_image.get("height", image_size[0])
            width = input_per_image.get("width", image_size[1])
            processed_results.append({})

            mask_pred_result = retry_if_cuda_oom(sem_seg_postprocess)(
                mask_pred_result, image_size, height, width
            )
            processed_results[-1]['grounding_mask'] = mask_pred_result

            # compute bbox
            # bbox = BitMasks(mask_pred_result > 0).get_bounding_boxes()
            # bbox = BoxMode.convert(bbox.tensor, BoxMode.XYXY_ABS, BoxMode.XYWH_ABS)
            # processed_results[-1]['grounding_box'] = bbox

        return processed_results

    def prepare_vlp_targets(self, batched_inputs, device):
        input_ids = []
        attention_mask = []
        for cnt, x in enumerate(batched_inputs):
            captions = x['captions']
            randid = random.randint(0, len(captions)-1)
            input_ids += x['tokens']['input_ids'][randid:randid+1]
            attention_mask += x['tokens']['attention_mask'][randid:randid+1]

        input_ids = torch.stack(input_ids)
        attention_mask = torch.stack(attention_mask)
        tokens = {"input_ids": input_ids, "attention_mask": attention_mask}
        lang_results = self.sem_seg_head.predictor.lang_encoder.get_text_token_embeddings(tokens, token=True)

        target_vlp = []
        for cnt, x in enumerate(batched_inputs):
            target_dict = {}
            target_dict["caption_tokens"] = lang_results['token_emb'][cnt:cnt+1]
            target_dict["caption_proj"] = lang_results['class_emb'][cnt:cnt+1]
            target_dict["caption_tokenids"] = lang_results['tokens']['input_ids'][cnt:cnt+1]
            target_dict["caption_mask"] = lang_results['tokens']['attention_mask'][cnt:cnt+1]            
            target_vlp.append(target_dict)
        return target_vlp
    
    def prepare_targets(self, batched_inputs, images, task = 'seg'):
        h_pad, w_pad = images.tensor.shape[-2:] # 获取图像的高宽尺寸
        new_targets = []
        for idx, batch_per_image in enumerate(batched_inputs):
            targets_per_image = batch_per_image["instances"].to(self.device) # 获取每个图片的目标

            if task != 'det':
                # pad gt
                gt_masks = targets_per_image.gt_masks # 获取真实的segmentation mask
                padded_masks = torch.zeros((gt_masks.shape[0], h_pad, w_pad), dtype=gt_masks.dtype, device=gt_masks.device) # 初始化一个全0的pad mask
                padded_masks[:, : gt_masks.shape[1], : gt_masks.shape[2]] = gt_masks # 将真实mask复制到pad mask中
            else:
                padded_masks = False

            # gt_boxes = targets_per_image.gt_boxes.tensor
            # ratio = torch.tensor([w_pad,h_pad,w_pad,h_pad]).to(gt_boxes.device)[None,:]
            # gt_boxes = gt_boxes / ratio # 根据宽高比例缩放bbox
            # xc,yc,w,h = (gt_boxes[:,0] + gt_boxes[:,2])/2, (gt_boxes[:,1] + gt_boxes[:,3])/2, gt_boxes[:,2] - gt_boxes[:,0], gt_boxes[:,3] - gt_boxes[:,1] # 计算bbox中心和宽高
            # gt_boxes = torch.stack([xc,yc,w,h]).permute(1,0) # 重新排列bbox格式

            h, w = targets_per_image.image_size
            image_size_xyxy = torch.as_tensor([w, h, w, h], dtype=torch.float, device=self.device)
            gt_boxes = box_ops.box_xyxy_to_cxcywh(targets_per_image.gt_boxes.tensor)/image_size_xyxy

            target_dict = {
                    "labels": targets_per_image.gt_classes,
                    "is_things": targets_per_image.is_things,
                    "masks": padded_masks,
                    "boxes": gt_boxes
                    }

            if self.task_switch['caption']:
                caption = batch_per_image["captions"] # 获取image的文字描述
                caption_noun = batch_per_image["captions_noun"] # 获取描述中的名词
                rand_index = random.randint(0, len(caption)-1) # 随机选择一个caption

                text = caption[rand_index]
                nouns = caption_noun[rand_index]
                noun_captions = [prompt_engineering(noun, topk=10000, suffix='.') for noun in nouns] + [text] # 对名词进行prompt engineering 选topk个prompt
                
                self.sem_seg_head.predictor.lang_encoder.get_text_embeddings(noun_captions, is_eval=False, name='caption_noun', prompt=False)
                ctext = getattr(self.sem_seg_head.predictor.lang_encoder, '{}_text_embeddings'.format('caption_noun'))
                target_dict["captions"] = ctext
                
                target_dict["captions_hash"] = [(hash(st.stem(txt)) % 10**16) for txt in (nouns + [text])] # 计算caption的hash 词
                target_dict["labels_hash"] = [(hash(st.stem(COCO_PANOPTIC_CLASSES[label_id].replace('-other','').replace('-merged','').replace('-stuff',''))) % 10**16) for label_id in target_dict['labels']]
                
            if self.task_switch['grounding']:
                # 获取grounding相关的数据
                grd_masks = batch_per_image['groundings']['masks']
                grd_texts = batch_per_image['groundings']['texts']
                grd_hash = batch_per_image['groundings']['hash']
                grd_task = batch_per_image['groundings']['mode']

                # 对masks padding到指定大小
                if len(grd_masks) == 0:
                    padded_masks = None
                else:
                    padded_masks = torch.zeros((grd_masks.shape[0], h_pad, w_pad), dtype=grd_masks.dtype, device=grd_masks.device)
                    padded_masks[:, : grd_masks.shape[1], : grd_masks.shape[2]] = grd_masks

                # 获取文本的词向量
                gtext = self.sem_seg_head.predictor.lang_encoder.get_text_token_embeddings(grd_texts, name='grounding', token=False, norm=False)
                token_emb = gtext['token_emb']
                tokens = gtext['tokens']

                # 只选择唯一的hash,去掉重复的
                unique_hash_id = np.unique(grd_hash, return_index=True)[1]
                selected_mask = np.zeros(len(grd_hash)).astype(np.bool)
                selected_mask[unique_hash_id] = True

                # 获取相关词向量
                selected_token_emb = token_emb[selected_mask]
                selected_attn_mask = tokens['attention_mask'][selected_mask]
                query_emb = selected_token_emb[selected_attn_mask.bool()]
                # 获取类别的词向量
                class_idx = tokens['attention_mask'].sum(dim=-1) - 1
                class_idx = torch.stack((torch.arange(len(class_idx), device=class_idx.device), class_idx)).tolist()
                class_emb = token_emb[class_idx]
                
                target_dict['grounding_masks'] = padded_masks
                target_dict['grounding_query_embs'] = query_emb
                target_dict['grounding_class_embs'] = class_emb
                target_dict['grounding_hash'] = grd_hash
                target_dict['grounding_task'] = grd_task

            new_targets.append(target_dict)
        return new_targets

    def semantic_inference(self, mask_cls, mask_pred, keep_sem_bgd=False):
        if keep_sem_bgd:
            mask_cls = F.softmax(mask_cls, dim=-1)
        else:
            mask_cls = F.softmax(mask_cls, dim=-1)[..., :-1]
        mask_pred = mask_pred.sigmoid()
        semseg = torch.einsum("qc,qhw->chw", mask_cls, mask_pred)
        return semseg

    def panoptic_inference(self, mask_cls, mask_pred):
        scores, labels = F.softmax(mask_cls, dim=-1).max(-1)
        mask_pred = mask_pred.sigmoid()

        keep = labels.ne(self.sem_seg_head.num_classes) & (scores > self.object_mask_threshold)
        cur_scores = scores[keep]
        cur_classes = labels[keep]
        cur_masks = mask_pred[keep]
        cur_mask_cls = mask_cls[keep]
        cur_mask_cls = cur_mask_cls[:, :-1]
        cur_prob_masks = cur_scores.view(-1, 1, 1) * cur_masks

        h, w = cur_masks.shape[-2:]
        panoptic_seg = torch.zeros((h, w), dtype=torch.int32, device=cur_masks.device)
        segments_info = []

        current_segment_id = 0

        if cur_masks.shape[0] == 0:
            # We didn't detect any mask :(
            return panoptic_seg, segments_info
        else:
            # take argmax
            cur_mask_ids = cur_prob_masks.argmax(0)
            stuff_memory_list = {}
            thing_dataset_id_to_contiguous_id = self.metadata.thing_dataset_id_to_contiguous_id if hasattr(self.metadata, 'thing_dataset_id_to_contiguous_id') else {}
            for k in range(cur_classes.shape[0]):
                pred_class = cur_classes[k].item()
                isthing = pred_class in thing_dataset_id_to_contiguous_id.values()
                mask_area = (cur_mask_ids == k).sum().item()
                original_area = (cur_masks[k] >= 0.5).sum().item()
                mask = (cur_mask_ids == k) & (cur_masks[k] >= 0.5)

                if mask_area > 0 and original_area > 0 and mask.sum().item() > 0:
                    if mask_area / original_area < self.overlap_threshold:
                        continue

                    # merge stuff regions
                    if not isthing:
                        if int(pred_class) in stuff_memory_list.keys():
                            panoptic_seg[mask] = stuff_memory_list[int(pred_class)]
                            continue
                        else:
                            stuff_memory_list[int(pred_class)] = current_segment_id + 1

                    current_segment_id += 1
                    panoptic_seg[mask] = current_segment_id

                    segments_info.append(
                        {
                            "id": current_segment_id,
                            "isthing": bool(isthing),
                            "category_id": int(pred_class),
                        }
                    )
            return panoptic_seg, segments_info

    def instance_inference(self, mask_cls, mask_pred, box_pred):
        # mask_pred is already processed to have the same shape as original input
        image_size = mask_pred.shape[-2:]

        # [Q, K]
        scores = F.softmax(mask_cls, dim=-1)[:, :-1]
        labels = torch.arange(self.sem_seg_head.num_classes, device=self.device).unsqueeze(0).repeat(self.num_queries, 1).flatten(0, 1)

        # scores_per_image, topk_indices = scores.flatten(0, 1).topk(self.num_queries, sorted=False)
        scores_per_image, topk_indices = scores.flatten(0, 1).topk(self.test_topk_per_image, sorted=False)

        labels_per_image = labels[topk_indices]
        topk_indices = (topk_indices // self.sem_seg_head.num_classes)
        # mask_pred = mask_pred.unsqueeze(1).repeat(1, self.sem_seg_head.num_classes, 1).flatten(0, 1)
        if mask_pred is not None:
            mask_pred = mask_pred[topk_indices]
        if box_pred is not None:
            box_pred = box_pred[topk_indices]

        # if this is panoptic segmentation, we only keep the "thing" classes
        if self.panoptic_on:
            thing_dataset_id_to_contiguous_id = self.metadata.thing_dataset_id_to_contiguous_id if hasattr(self.metadata, 'thing_dataset_id_to_contiguous_id') else {}
            keep = torch.zeros_like(scores_per_image).bool()
            for i, lab in enumerate(labels_per_image):
                keep[i] = lab in thing_dataset_id_to_contiguous_id.values()

            scores_per_image = scores_per_image[keep]
            labels_per_image = labels_per_image[keep]
            mask_pred = mask_pred[keep]

            if box_pred is not None:
                box_pred = box_pred[keep]

        result = Instances(image_size)
        # mask (before sigmoid)
        result.pred_masks = (mask_pred > 0).float()

        # Uncomment the following to get boxes from masks (this is slow)

        if box_pred is not None:
            # result.pred_boxes = BitMasks(mask_pred > 0).get_bounding_boxes()
            result.pred_boxes = Boxes(box_pred)

        else:
            result.pred_boxes = BitMasks(mask_pred > 0).get_bounding_boxes()

        # calculate average mask prob
        mask_scores_per_image = (mask_pred.sigmoid().flatten(1) * result.pred_masks.flatten(1)).sum(1) / (result.pred_masks.flatten(1).sum(1) + 1e-6)
        result.scores = scores_per_image * mask_scores_per_image
        result.pred_classes = labels_per_image

        return result



@register_model
def get_xdecoder_model(cfg, **kwargs):
    return GeneralizedXdecoderPlus(cfg)