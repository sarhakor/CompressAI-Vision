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

from typing import Any, Dict

import numpy as np
import torch
from scipy.stats import norm

import deepCABAC
from compressai_vision.codecs.encdec_utils import *

from .common import FeatureTensorCodingType
from .hls import SequenceParameterSet

__all__ = [
    "intra_coding",
    "intra_decoding",
]


def _quantize_and_encode(channels, qp, qp_density, maxValue=-1):
    # TODO (fracape) check perf vs cabac per channel
    encoder = deepCABAC.Encoder()  # deepCABAC Encoder
    encoder.initCtxModels(10, 0)  # TODO: @eimran Should we change this?

    # print(channels[0, :10, :10])
    quantizedValues = np.zeros(channels.shape, dtype=np.int32)
    encoder.quantFeatures(
        channels.detach().cpu().numpy(), quantizedValues, qp_density, qp, 0
    )  # TODO: @eimran change scan order and qp method

    # print(quantizedValues[0, :10, :10])
    encoder.encodeFeatures(quantizedValues, 0, maxValue)
    bs = bytearray(encoder.finish().tobytes())
    total_bytes_spent = len(bs)
    # return quantized values for debugging
    return total_bytes_spent, bs, quantizedValues


def _dequantize_and_decode(data_shape, bs, qp, qp_density):
    tC, tH, tW = data_shape

    assert isinstance(bs, (bytearray, bytes))

    if not isinstance(bs, bytearray):
        bs = bytearray(bs)

    # need to decode max_value
    max_value = -1

    decoder = deepCABAC.Decoder()
    decoder.initCtxModels(10)
    decoder.setStream(bs)
    quantizedValues = np.zeros((tC, tH, tW), dtype=np.int32)
    decoder.decodeFeatures(quantizedValues, 0, max_value)
    # print(quantizedValues[0, :10, :10])
    recon_features = np.zeros(quantizedValues.shape, dtype=np.float32)
    decoder.dequantFeatures(
        recon_features, quantizedValues, qp_density, qp, 0
    )  # TODO: @eimran send qp with bitstream
    # print(recon_features[0, :10, :10])

    return recon_features


def intra_coding(
    sps: SequenceParameterSet,
    ftensors: Dict,
    all_coding_groups: Dict,
    bitstream_fd: Any,
):
    byte_cnt = 0

    # temporary hard coded
    nb_sigmas = {"p2": 1, "p3": 1, "p4": 3, "p5": 3}

    byte_cnt += write_uchars(bitstream_fd, (FeatureTensorCodingType.I_TYPE.value,))

    recon_ftensors = {}
    for tag, ftensor in ftensors.items():
        coding_groups = all_coding_groups[tag]

        # Not compressing the vector channels_coding_modes for now: can use cabac and
        # differential coding
        # encode number of clusters kept
        # TODO (fracape) could it be retrieved/derived from channels_coding_modes
        assert (
            coding_groups.max() + 1
        ) < 256, "too many clusters, currenlty coding nb clusters on one byte"

        byte_cnt += write_uchars(bitstream_fd, coding_groups + 1)

        # sigma clipping
        mu = ftensor.mean()
        std = ftensor.std()

        min_clip = mu - (nb_sigmas[tag] * std)
        max_clip = mu + (nb_sigmas[tag] * std)

        assert max_clip >= 0

        # TODO (fracape) check best method, for now, clip at -max, max
        clip_bnd = max(abs(min_clip), max_clip)

        clipped_ftensor = ftensor.clamp(min=-clip_bnd, max=clip_bnd)
        # sigma clipping done

        (
            byte_spent,
            byte_array,
            quantized_ftensor,
        ) = _quantize_and_encode(clipped_ftensor, sps.qp, sps.qp_density)

        byte_cnt += write_uints(bitstream_fd, (byte_spent,))
        byte_cnt += byte_spent
        bitstream_fd.write(byte_array)

        # de-quantize for reconstructed feature tensor
        decoder = deepCABAC.Decoder()
        decoder.initCtxModels(10)

        dequantized_ftensor = np.zeros(quantized_ftensor.shape, dtype=np.float32)
        decoder.dequantFeatures(
            dequantized_ftensor, quantized_ftensor, sps.qp_density, sps.qp, 0
        )  # TODO: @eimran send qp with bitstream
        # print(dequantized_ftensor[0, :10, :10])

        recon_ftensors[tag] = dequantized_ftensor

    return byte_cnt, recon_ftensors


def intra_decoding(sps: SequenceParameterSet, bitstream_fd: Any):
    recon_ftensors = {}
    for e, shape_of_ftensor in enumerate(sps.shapes_of_features):
        C, H, W = shape_of_ftensor.values()
        coding_groups = read_uchars(bitstream_fd, C)
        coding_groups = np.array(coding_groups) - 1

        self_coded_labels = np.where(coding_groups == -1)[0]
        group_coded_labels = np.unique(coding_groups[np.where(coding_groups != -1)])

        # create channel collections
        channel_collections = {}
        for group_label in group_coded_labels:
            channel_ids = list(np.where(coding_groups == group_label)[0])
            channel_collections[min(channel_ids)] = channel_ids

        for label in self_coded_labels:
            channel_collections[label] = [label]

        channel_collections = dict(
            sorted(channel_collections.items(), key=lambda item: item[0])
        )

        nb_channels_coded_ftensor = len(channel_collections)
        # - 1

        byte_to_read = read_uints(bitstream_fd, 1)[0]
        byte_array = bitstream_fd.read(byte_to_read)

        dequantized_coded_ftensor = _dequantize_and_decode(
            (nb_channels_coded_ftensor, H, W), byte_array, sps.qp, sps.qp_density
        )
        dequantized_coded_ftensor = torch.from_numpy(dequantized_coded_ftensor)

        recon_ftensor = torch.zeros((C, H, W), dtype=torch.float32)
        for channels, ftensor in zip(
            channel_collections.values(), dequantized_coded_ftensor
        ):
            recon_ftensor[channels] = ftensor

        recon_ftensors[e] = recon_ftensor

    return recon_ftensors
