# Copyright (c) 2022-2024, InterDigital Communications, Inc
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted (subject to the limitations in the disclaimer
# below) provided that the following conditions are met:

# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# * Neither the name of InterDigital Communications, Inc nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.

# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT
# NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import json
import math
from collections import defaultdict
from pathlib import Path

import motmetrics as mm
import numpy as np
import pandas as pd
import torch
from detectron2.evaluation import COCOEvaluator
from jde.utils.io import unzip_objs
from pycocotools.coco import COCO
from pytorch_msssim import ms_ssim
from tqdm import tqdm
from yolox.data.datasets.coco import remove_useless_info
from yolox.evaluators import COCOEvaluator as YOLOX_COCOEvaluator

from compressai_vision.datasets import deccode_compressed_rle
from compressai_vision.registry import register_evaluator
from compressai_vision.utils import time_measure, to_cpu

from .base_evaluator import BaseEvaluator
from .tf_evaluation_utils import (
    DetectionResultFields,
    InputDataFields,
    OpenImagesChallengeEvaluator,
    decode_gt_raw_data_into_masks_and_boxes,
    decode_masks,
    encode_masks,
)


@register_evaluator("COCO-EVAL")
class COCOEVal(BaseEvaluator):
    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="AP",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.set_annotation_info(dataset)

        self._evaluator = COCOEvaluator(
            dataset_name, False, output_dir=output_dir, use_fast_impl=False
        )

        if datacatalog_name == "MPEGOIV6":
            deccode_compressed_rle(self._evaluator._coco_api.anns)

        self.reset()

    def reset(self):
        self._evaluator.reset()

    def digest(self, gt, pred):
        return self._evaluator.process(gt, pred)

    def results(self, save_path: str = None):
        out = self._evaluator.evaluate()

        if save_path:
            self.write_results(out, save_path)

        self.write_results(out)

        # summary = {}
        # for key, item_dict in out.items():
        #     summary[f"{key}"] = item_dict["AP"]

        return out


@register_evaluator("OIC-EVAL")
class OpenImagesChallengeEval(BaseEvaluator):
    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="AP50",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.set_annotation_info(dataset)

        with open(dataset.annotation_path) as f:
            json_dict = json.load(f)

        def _search_category(name):
            for e, item in enumerate(self.thing_classes):
                if name == self._normalize_labelname(item):
                    return e

            self._logger.error(f"Not found Item {name} in 'thing_classes'")
            raise ValueError

        assert len(json_dict["annotations"]) > 0
        has_segmentation = (
            True if "segmentation" in json_dict["annotations"][0] else False
        )

        self._valid_contiguous_id = []
        self._oic_labelmap_dict = {}
        self._oic_categories = []

        assert "oic_labelmap" in json_dict
        assert "oic_annotations" in json_dict

        valid_categories = json_dict["oic_labelmap"]
        gt_annotations = json_dict["oic_annotations"]

        for item in valid_categories:
            e = _search_category(self._normalize_labelname(item["name"]))

            if e is not None:
                self._valid_contiguous_id.append(e)

            self._oic_labelmap_dict[self._normalize_labelname(item["name"])] = item[
                "id"
            ]
            self._oic_categories.append({"id": item["id"], "name": item["name"]})

        self._oic_evaluator = OpenImagesChallengeEvaluator(
            self._oic_categories, evaluate_masks=has_segmentation
        )

        self._logger.info(
            f"Loading annotations for {len(gt_annotations)} images and reformatting them for OpenImageChallenge Evaluator."
        )
        for gt in tqdm(gt_annotations):
            img_id = gt["image_id"]
            annotations = gt["annotations"]

            anno_dict = {}
            anno_dict[InputDataFields.groundtruth_boxes] = np.array(annotations["bbox"])
            anno_dict[InputDataFields.groundtruth_classes] = np.array(
                annotations["cls"]
            )
            anno_dict[InputDataFields.groundtruth_group_of] = np.array(
                annotations["group_of"]
            )

            img_cls = []
            for name in annotations["img_cls"]:
                img_cls.append(self._oic_labelmap_dict[self._normalize_labelname(name)])
            anno_dict[InputDataFields.groundtruth_image_classes] = np.array(img_cls)

            if "mask" in annotations:
                segments, _ = decode_gt_raw_data_into_masks_and_boxes(
                    annotations["mask"], annotations["img_size"]
                )
                anno_dict[InputDataFields.groundtruth_instance_masks] = segments

            self._oic_evaluator.add_single_ground_truth_image_info(img_id, anno_dict)

        self._logger.info(
            f"All groundtruth annotations for {len(gt_annotations)} images are successfully registred to the evaluator."
        )

        self.reset()

    def reset(self):
        self._predictions = []
        self._cc = 0

    @staticmethod
    def _normalize_labelname(name: str):
        return name.lower().replace(" ", "_")

    def digest(self, gt, pred):
        assert len(gt) == len(pred) == 1, "Batch size must be 1 for the evaluation"

        if self._oic_evaluator is None:
            self._logger.warning(
                "There is no assigned evaluator for the class. Evaluator will not work properly"
            )
            return

        img_id = gt[0]["image_id"]
        test_fields = to_cpu(pred[0]["instances"])

        imgH, imgW = test_fields.image_size

        classes = test_fields.pred_classes
        scores = test_fields.scores
        bboxes = test_fields.pred_boxes.tensor

        assert len(classes) == len(scores) == len(bboxes)

        masks = []
        has_mask = test_fields.has("pred_masks")
        if has_mask:
            masks = test_fields.pred_masks
            assert len(masks) == len(bboxes)

        valid_indexes = []
        for e, cid in enumerate(classes):
            if cid in self._valid_contiguous_id:
                valid_indexes.append(e)

        if len(valid_indexes) > 0:
            pred_dict = {
                "img_id": img_id,
                "img_size": (imgH, imgW),
                "classes": classes[valid_indexes].tolist(),
                "scores": scores[valid_indexes].tolist(),
                "bboxes": bboxes[valid_indexes].tolist(),
            }
            if has_mask:
                pred_dict["masks"] = encode_masks(masks[valid_indexes])

            self._predictions.append(pred_dict)

        self._cc += 1

        return

    def _process_prediction(self, pred_dict):
        valid_cls = []
        valid_scores = []
        valid_bboxes = []
        valid_segment_masks = []
        valid_segment_boxes = []

        imgH, imgW = pred_dict["img_size"]
        classes = pred_dict["classes"]
        scores = pred_dict["scores"]
        bboxes = pred_dict["bboxes"]

        has_mask = True if "masks" in pred_dict else False
        if has_mask:
            masks = pred_dict["masks"]

        for e, _items in enumerate(zip(classes, scores, bboxes)):
            _class, _score, _bbox = _items

            cate_name = self._normalize_labelname(self.thing_classes[_class])
            valid_cls.append(self._oic_labelmap_dict[cate_name])

            valid_scores.append(_score)
            norm_bbox = np.array(_bbox) / [imgW, imgH, imgW, imgH]
            # XMin, YMin, XMax, YMax --> YMin, XMin, YMax, XMax
            valid_bboxes.append(norm_bbox[[1, 0, 3, 2]])

            if has_mask:
                segment, boxe = decode_masks(masks[e])
                valid_segment_masks.append(segment)
                valid_segment_boxes.append(boxe)

        res_dict = {
            DetectionResultFields.detection_classes: np.array(valid_cls),
            DetectionResultFields.detection_scores: np.array(valid_scores).astype(
                float
            ),
        }

        if has_mask:
            res_dict[DetectionResultFields.detection_masks] = np.concatenate(
                valid_segment_masks, axis=0
            )
            res_dict[DetectionResultFields.detection_boxes] = np.concatenate(
                valid_segment_boxes, axis=0
            )
        else:
            res_dict[DetectionResultFields.detection_boxes] = np.array(
                valid_bboxes
            ).astype(float)

        return res_dict

    def results(self, save_path: str = None):
        if self._oic_evaluator is None:
            self._logger.warning(
                "There is no assigned evaluator for the class. Evaluator will not work properly"
            )
            return

        if len(self._predictions) == 0:
            self._logger.warning("There is no detected objects to evaluate")
            return

        start = time_measure()
        for pred_dict in self._predictions:
            img_id = pred_dict["img_id"]
            processed_dict = self._process_prediction(pred_dict)
            self._oic_evaluator.add_single_detected_image_info(img_id, processed_dict)

        self._logger.info(
            f"Elapsed time to process and register the predicted items to the evaluator: {time_measure() - start:.02f} sec"
        )
        out = self._oic_evaluator.evaluate()
        self._logger.info(f"Total evaluation time: {time_measure() - start:.02f} sec")

        if save_path:
            self.write_results(out, save_path)

        self.write_results(out)

        summary = {}
        for key, value in out.items():
            name = "-".join(key.split("/")[1:])
            summary[name] = value

        return summary


@register_evaluator("MOT-JDE-EVAL")
class MOT_JDE_Eval(BaseEvaluator):
    """
    A Multiple Object Tracking Evaluator

    This class evaluates MOT performance of tracking model such as JDE in compressai-vision.
    BaseEvaluator is inherited to interface with pipeline architecture in compressai-vision

    Functions below in this class refers to
        The class Evaluator inin Towards-Realtime-MOT/utils/evaluation.py at
        <https://github.com/Zhongdao/Towards-Realtime-MOT/blob/master/utils/evaluation.py>
        <https://github.com/Zhongdao/Towards-Realtime-MOT/blob/master/track.py>

        Full license statement can be found at
        <https://github.com/Zhongdao/Towards-Realtime-MOT/blob/master/LICENSE>

    """

    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="MOTA",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.set_annotation_info(dataset)

        mm.lap.default_solver = "lap"
        self.dataset = dataset.dataset
        self.eval_info_file_name = self.get_jde_eval_info_name(self.dataset_name)

        self.reset()

    def reset(self):
        self.acc = mm.MOTAccumulator(auto_id=True)
        self._predictions = {}

    @staticmethod
    def _load_gt_in_motchallenge(filepath, fmt="mot15-2D", min_confidence=-1):
        return mm.io.loadtxt(filepath, fmt=fmt, min_confidence=min_confidence)

    @staticmethod
    def _format_pd_in_motchallenge(predictions: dict):
        all_indexes = []
        all_columns = []

        for frmID, preds in predictions.items():
            if len(preds) > 0:
                for data in preds:
                    tlwh, objID, conf = data

                    all_indexes.append((frmID, objID))

                    column_data = tlwh + (conf, -1, -1)
                    all_columns.append(column_data)

        columns = ["X", "Y", "Width", "Height", "Confidence", "ClassId", "Visibility"]

        idx = pd.MultiIndex.from_tuples(all_indexes, names=["FrameId", "Id"])
        return pd.DataFrame(all_columns, idx, columns)

    def digest(self, gt, pred):
        pred_list = []
        for tlwh, id in zip(pred["tlwhs"], pred["ids"]):
            x1, y1, w, h = tlwh
            x2, y2 = x1 + w, y1 + h
            parsed_pred = ((x1, y1, w, h), id, 1.0)
            pred_list.append(parsed_pred)

        self._predictions[int(gt[0]["image_id"])] = pred_list

    def results(self, save_path: str = None):
        out = self.mot_eval()

        if save_path:
            self.write_results(out, save_path)

        self.write_results(out)

        return out

    @staticmethod
    def digest_summary(summary):
        ret = {}
        keys_lists = [
            (
                [
                    "idf1",
                    "idp",
                    "idr",
                    "recall",
                    "precision",
                    "num_unique_objects",
                    "mota",
                    "motp",
                ],
                None,
            ),
            (
                [
                    "mostly_tracked",
                    "partially_tracked",
                    "mostly_lost",
                    "num_false_positives",
                    "num_misses",
                    "num_switches",
                    "num_fragmentations",
                    "num_transfer",
                    "num_ascend",
                    "num_migrate",
                ],
                int,
            ),
        ]

        for keys, dtype in keys_lists:
            selected_keys = [key for key in keys if key in summary]
            for key in selected_keys:
                ret[key] = summary[key] if dtype is None else dtype(summary[key])

        return ret

    def mot_eval(self):
        assert len(self.dataset) == len(
            self._predictions
        ), "Total number of frames are mismatch"

        # skip the very first frame
        for gt_frame in self.dataset[1:]:
            frm_id = int(gt_frame["image_id"])

            pred_objs = self._predictions[frm_id].copy()
            pred_tlwhs, pred_ids, _ = unzip_objs(pred_objs)

            gt_objs = gt_frame["annotations"]["gt"].copy()
            gt_tlwhs, gt_ids, _ = unzip_objs(gt_objs)

            gt_ignore = gt_frame["annotations"]["gt_ignore"].copy()
            gt_ignore_tlwhs, _, _ = unzip_objs(gt_ignore)

            # remove ignored results
            keep = np.ones(len(pred_tlwhs), dtype=bool)
            iou_distance = mm.distances.iou_matrix(
                gt_ignore_tlwhs, pred_tlwhs, max_iou=0.5
            )
            if len(iou_distance) > 0:
                match_is, match_js = mm.lap.linear_sum_assignment(iou_distance)
                match_is, match_js = map(
                    lambda a: np.asarray(a, dtype=int), [match_is, match_js]
                )
                match_ious = iou_distance[match_is, match_js]

                match_js = np.asarray(match_js, dtype=int)
                match_js = match_js[np.logical_not(np.isnan(match_ious))]
                keep[match_js] = False
                pred_tlwhs = pred_tlwhs[keep]
                pred_ids = pred_ids[keep]

            # get distance matrix
            iou_distance = mm.distances.iou_matrix(gt_tlwhs, pred_tlwhs, max_iou=0.5)

            # accumulate
            self.acc.update(gt_ids, pred_ids, iou_distance)

        # get summary
        metrics = mm.metrics.motchallenge_metrics
        mh = mm.metrics.create()

        summary = mh.compute(
            self.acc,
            metrics=metrics,
            name=self.dataset_name,
            return_dataframe=False,
            return_cached=True,
        )

        return self.digest_summary(summary)

    def _save_all_eval_info(self, pred: dict):
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        file_name = f"{self.output_dir}/{self.eval_info_file_name}"
        torch.save(pred, file_name)

    def _load_all_eval_info(self):
        file_name = f"{self.output_dir}/{self.eval_info_file_name}"
        return torch.load(file_name)


@register_evaluator("MOT-TVD-EVAL")
class MOT_TVD_Eval(MOT_JDE_Eval):
    """
    A Multiple Object Tracking Evaluator for TVD

    This class evaluates MOT performance of tracking model such as JDE specifically on TVD
    """

    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="MOTA",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.set_annotation_info(dataset)

        self._gt_pd = self._load_gt_in_motchallenge(self.annotation_path)

        assert self.seqinfo_path is not None, "Sequence Information must be provided"

    def mot_eval(self):
        assert len(self.dataset) == len(
            self._predictions
        ), "Total number of frames are mismatch"

        self._save_all_eval_info(self._predictions)
        _pd_pd = self._format_pd_in_motchallenge(self._predictions)

        acc, ana = mm.utils.CLEAR_MOT_M(self._gt_pd, _pd_pd, self.seqinfo_path)

        # get summary
        metrics = mm.metrics.motchallenge_metrics
        mh = mm.metrics.create()

        summary = mh.compute(
            acc,
            ana=ana,
            metrics=metrics,
            name=self.dataset_name,
            return_dataframe=False,
            return_cached=True,
        )

        return self.digest_summary(summary)


@register_evaluator("MOT-HIEVE-EVAL")
class MOT_HiEve_Eval(MOT_JDE_Eval):
    """
    A Multiple Object Tracking Evaluator for HiEve

    This class evaluates MOT performance of tracking model such as JDE specifically on HiEve

    """

    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="MOTA",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.set_annotation_info(dataset)

        mm.lap.default_solver = "munkres"

        self._gt_pd = self._load_gt_in_motchallenge(
            self.annotation_path, min_confidence=1
        )

    def mot_eval(self):
        assert len(self.dataset) == len(
            self._predictions
        ), "Total number of frames are mismatch"

        self._save_all_eval_info(self._predictions)
        _pd_pd = self._format_pd_in_motchallenge(self._predictions)

        acc = mm.utils.compare_to_groundtruth(self._gt_pd, _pd_pd)

        # get summary
        metrics = mm.metrics.motchallenge_metrics
        mh = mm.metrics.create()

        summary = mh.compute(
            acc,
            metrics=metrics,
            name=self.dataset_name,
            return_dataframe=False,
            return_cached=True,
        )

        return self.digest_summary(summary)


@register_evaluator("YOLOX-COCO-EVAL")
class YOLOXCOCOEval(BaseEvaluator):
    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="AP",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.set_annotation_info(dataset)

        cocoapi = COCO(self.annotation_path)
        remove_useless_info(cocoapi)
        class_ids = sorted(cocoapi.getCatIds())
        cats = cocoapi.loadCats(cocoapi.getCatIds())

        class dummy_dataloader:
            def __init__(self):
                class dummy_dataset:
                    def __init__(self):
                        self.coco = cocoapi
                        self.class_ids = class_ids
                        self.cats = cats

                self.dataset = dummy_dataset()
                self.batch_size = 1

        dataloader = dummy_dataloader()
        self._evaluator = YOLOX_COCOEvaluator(
            dataloader, dataset.input_size, -1, -1, -1
        )
        self.reset()

    def reset(self):
        self.data_list = []
        self.output_data = defaultdict()

    def digest(self, gt, pred):
        assert len(gt) == 1

        img_heights = [gt[0]["height"]]
        img_widths = [gt[0]["width"]]
        img_ids = [gt[0]["image_id"]]

        data_list_elem, image_wise_data = self._evaluator.convert_to_coco_format(
            pred, [img_heights, img_widths], img_ids, return_outputs=True
        )
        self.data_list.extend(data_list_elem)
        self.output_data.update(image_wise_data)

    def results(self, save_path: str = None):
        dummy_statistics = torch.FloatTensor([0, 0, len(self.output_data)])
        eval_results = self._evaluator.evaluate_prediction(
            self.data_list, dummy_statistics
        )

        if save_path:
            self.write_results(eval_results, save_path)

        self.write_results(eval_results)

        *listed_items, summary = eval_results

        self._logger.info("\n" + summary)

        return {"AP": listed_items[0] * 100, "AP50": listed_items[1] * 100}


@register_evaluator("VISUAL-QUALITY-EVAL")
class VisualQualityEval(BaseEvaluator):
    def __init__(
        self,
        datacatalog_name,
        dataset_name,
        dataset,
        output_dir="./vision_output/",
        criteria="psnr",
    ):
        super().__init__(datacatalog_name, dataset_name, dataset, output_dir, criteria)

        self.reset()

    @staticmethod
    def compute_psnr(a, b):
        mse = torch.mean((a - b) ** 2).item()
        return -10 * math.log10(mse)

    @staticmethod
    def compute_msssim(a, b):
        return ms_ssim(a, b, data_range=1.0).item()

    def reset(self):
        self._evaluations = []
        self._sum_psnr = 0
        self._sum_msssim = 0
        self._cc = 0

    def write_results(self, path: str = None):
        if path is None:
            path = f"{self.output_dir}"

        path = Path(path)
        if not path.is_dir():
            self._logger.info(f"creating output folder: {path}")
            path.mkdir(parents=True, exist_ok=True)

        with open(f"{path}/{self.output_file_name}.json", "w", encoding="utf-8") as f:
            json.dump(self._evaluations, f, ensure_ascii=False, indent=4)

    def digest(self, gt, pred):
        ref = gt[0]["image"].unsqueeze(0).cpu()
        tst = pred.unsqueeze(0).cpu()

        assert ref.shape == tst.shape

        psnr = self.compute_psnr(ref, tst)
        msssim = self.compute_msssim(ref, tst)

        eval_dict = {
            "img_id": gt[0]["image_id"],
            "img_size": (gt[0]["height"], gt[0]["width"]),
            "msssim": msssim,
            "psnr": psnr,
        }

        self._sum_psnr += psnr
        self._sum_msssim += msssim
        self._evaluations.append(eval_dict)

        self._cc += 1

    def results(self, save_path: str = None):
        if save_path:
            self.write_results(save_path)

        self.write_results()

        summary = {
            "msssim": (self._sum_msssim / self._cc),
            "psnr": (self._sum_psnr / self._cc),
        }
        return summary
