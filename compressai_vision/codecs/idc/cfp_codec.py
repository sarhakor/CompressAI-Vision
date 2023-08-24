# Copyright (c) 2022-2023, InterDigital Communications, Inc
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

import logging
from pathlib import Path
from typing import Dict, List, Union

import numpy as np
import torch
import torch.nn as nn
from tqdm import tqdm

from compressai_vision.registry import register_codec

from .common import FeatureTensorCodingType
from .hls import SequenceParameterSet, parse_feature_tensor_coding_type
from .inter import inter_coding, inter_decoding
from .intra import intra_coding, intra_decoding
from .tools import feature_channel_suppression, search_for_N_clusters

code_feature_tensor = {
    FeatureTensorCodingType.I_TYPE: intra_coding,
    FeatureTensorCodingType.PB_TYPE: inter_coding,
}

decode_feature_tensor = {
    FeatureTensorCodingType.I_TYPE: intra_decoding,
    FeatureTensorCodingType.PB_TYPE: inter_decoding,
}


def iterate_list_of_tensors(data: Dict):
    list_of_features_sets = list(data.values())
    list_of_keys = list(data.keys())

    num_feature_sets = list_of_features_sets[0].size(0)

    if any(fs.size(0) != num_feature_sets for fs in list_of_features_sets):
        raise ValueError("Feature set items must have the same number of features sets")

    for current_feature_set in tqdm(
        zip(*list_of_features_sets), total=num_feature_sets
    ):
        yield dict(zip(list_of_keys, current_feature_set))


@register_codec("cfp_codec")
class CFP_CODEC(nn.Module):
    """
    CfP  encoder
    """

    def __init__(
        self,
        **kwargs,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)

        self.enc_cfg = kwargs["encoder_config"]
        self.dec_cfg = kwargs["decoder_config"]

        self.deep_feature_proxy = kwargs["vision_model"].deep_feature_proxy

        self.device = kwargs["vision_model"].device

        self.qp = self.enc_cfg["qp"]
        self.qp_density = self.enc_cfg["qp_density"]
        self.eval_encode = kwargs["eval_encode"]

        assert (
            self.enc_cfg["qp"] is not None
        ), "Please provide a QP value!"  # TODO: @eimran maybe run the process to get uncmp result

        self.bitstream_dir = Path(kwargs["bitstream_dir"])
        if not self.bitstream_dir.is_dir():
            self.bitstream_dir.mkdir(parents=True, exist_ok=True)

        # encoder parameters & buffers
        self.reset()

    def reset(self):
        self.feature_set_order_count = -1
        self.decoded_tensor_buffer = []
        self._bitstream_path = None
        self._bitstream_fd = None

    @property
    def qp_value(self):
        return self.enc_cfg["qp"]

    @property
    def eval_encode_type(self):
        return self.eval_encode

    def set_bitstream_path(self, fname, mode="rb"):
        self._bitstream_path = self.bitstream_dir / f"{fname}"
        fd = self.open_bitstream_file(self._bitstream_path, mode)
        return fd

    def open_bitstream_file(self, path, mode="rb"):
        self._bitstream_fd = open(path, mode)
        return self._bitstream_fd

    def close_files(self):
        if self._bitstream_fd:
            self._bitstream_fd.close()

    @property
    def bitstream_path(self):
        return self._bitstream_path

    def encode(
        self,
        input: Dict,
        file_prefix: str = "",
    ) -> Dict:
        byte_cnt = 0

        self.logger.info("Encoding starts...")

        # check Layers lengths
        layer_nbframes = [
            layer_data.size()[0] for _, layer_data in input["data"].items()
        ]
        assert all(n == layer_nbframes[0] for n in layer_nbframes)
        nbframes = layer_nbframes[0]
        # nbframes = 2  # for debugging

        if file_prefix == "":
            file_prefix = self.enc_cfg["output_bitstream"].split(".")[0]

        bitstream_fd = self.set_bitstream_path(f"{file_prefix}.bin", "wb")

        # parsing encoder configurations
        intra_period = self.enc_cfg["intra_period"]
        got_size = self.enc_cfg["group_of_tensor"]
        n_bits = 8

        sps = SequenceParameterSet()
        sps.digest(**input)

        # write sps
        # TODO (fracape) nbframes, qp, qp_density are temporary syntax.
        # These are removed later
        byte_cnt += sps.write(bitstream_fd, nbframes, self.qp, self.qp_density)

        for feature_tensor in iterate_list_of_tensors(input["data"]):
            # counting one for the input
            self.feature_set_order_count += 1  # the same concept as poc

            eFTCType = FeatureTensorCodingType.PB_TYPE
            # All intra when intra_period == -1
            if intra_period == -1 or (self.feature_set_order_count % intra_period) == 0:
                eFTCType = FeatureTensorCodingType.I_TYPE

                channel_collections_by_cluster = search_for_N_clusters(
                    feature_tensor, self.deep_feature_proxy
                )

            (
                feature_channels_to_code,
                all_channels_coding_groups,
            ) = feature_channel_suppression(
                feature_tensor, channel_collections_by_cluster
            )

            byte_spent, recon_feature_channels = code_feature_tensor[eFTCType](
                sps, feature_channels_to_code, all_channels_coding_groups, bitstream_fd
            )
            byte_cnt += byte_spent

        self.close_files()

        # TODO (fracape) give a clearer returned objects for bitstream
        # actual tensors in base, bin file in anchors and quantized data for debug here
        #  homogenize with other codecs
        print(f"bpp: {(byte_cnt * 8) / (nbframes * sps.org_input_height * sps.org_input_width)}")

        return {
            "bytes": [
                byte_cnt,
            ],
            "bitstream": self.bitstream_path,
        }

    def decode(
        self,
        input: str,
        file_prefix: str = "",
    ):
        self.logger.info("Decoding starts...")

        output = {}

        bitstream_fd = self.open_bitstream_file(input, "rb")

        sps = SequenceParameterSet()

        # read sequence parameter set
        sps.read(bitstream_fd)

        output = {
            "org_input_size": {
                "height": sps.org_input_height,
                "width": sps.org_input_width,
            },
            "input_size": [(sps.input_height, sps.input_width)],
        }

        # TODO (fracape) is the layer structure
        # and nb_channel per layer supposed to be known by decoder?
        # can be added in bitstrea
        # model id is tricky, since split point and other infos could be necessary
        model_name = "faster_rcnn_X_101_32x8d_FPN_3x"
        # "faster_rcnn_X_101_32x8d_FPN_3x",
        # "mask_rcnn_X_101_32x8d_FPN_3x",
        # "faster_rcnn_R_50_FPN_3x",
        # "mask_rcnn_R_50_FPN_3x",
        # "jde_1088x608",

        # TODO (fracape) implement  jde case
        if "rcnn" in model_name:
            ftensor_tags = ["p2", "p3", "p4", "p5"]
            assert sps.size_of_feature_set == len(ftensor_tags)

        recon_ftensors = dict(zip(ftensor_tags, [[] for _ in range(len(ftensor_tags))]))
        for ftensor_set_idx in range(sps.nbframes):
            # print(ftensor_set_idx)

            # read coding type
            eFTCType = parse_feature_tensor_coding_type(bitstream_fd)
            res = decode_feature_tensor[eFTCType](sps, bitstream_fd)

            for tlist, item in zip(recon_ftensors.values(), res.values()):
                tlist.append(item)
            # print(eFTCType)
            # print(eFTCType, ftensor_set_idx)

        self.close_files()

        for key, item in recon_ftensors.items():
            recon_ftensors[key] = torch.stack(item)

        output["data"] = recon_ftensors

        return output

    # ??
    def _subtract_mean_from_each_ch(self, representative_samples_dict):
        mean_dict = {}
        result = {}
        for cluster_no, representative_sample in representative_samples_dict.items():
            mean_ch = np.mean(representative_sample)
            result[cluster_no] = representative_sample - mean_ch
            mean_dict[cluster_no] = mean_ch
        return result, mean_dict

    def _add_mean_to_each_ch(self, representative_samples_dict, mean_dict):
        result = {}
        for cluster_no, representative_sample in representative_samples_dict.items():
            result[cluster_no] = representative_sample + mean_dict[cluster_no]
        return result
