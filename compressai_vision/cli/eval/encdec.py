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

from __future__ import annotations

from typing import Any

import hydra
from omegaconf import DictConfig

from compressai_vision.config import (
    configure_conf,
    create_codec,
    create_dataloader,
    create_evaluator,
    create_pipline,
    create_vision_model,
)


def setup(conf: DictConfig) -> dict[str, Any]:
    # how to properly logs...?

    configure_conf(conf)

    vision_model = create_vision_model(conf.misc.device, conf.vision_model)
    dataloader = create_dataloader(conf.dataset, conf.misc.device, vision_model.cfg)
    evaluator = create_evaluator(
        conf.evaluator,
        conf.dataset.datacatalog,
        conf.dataset.config.dataset_name,
        dataloader.dataset,
    )

    codec = create_codec(conf.codec)

    pipeline = create_pipline(conf.pipeline, vision_model, codec, dataloader, evaluator)

    return pipeline


@hydra.main(version_base=None, config_path="cfgs")
def main(conf: DictConfig):
    pipeline = setup(conf)

    ret = pipeline()

    # summarize results


if __name__ == "__main__":
    main()
