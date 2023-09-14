#!/usr/bin/env bash
set -eu
export DNNL_MAX_CPU_ISA=AVX2

VCM_TESTDATA=$1
OUTPUT_DIR=$2
EXPERIMENT=$3
DEVICE=$4
AP=$5
CODEC_PARAMS=$6

echo ${VCM_TESTDATA}, ${OUTPUT_DIR}, ${EXPERIMENT}, ${DEVICE}, ${AP}, ${CODEC_PARAMS}

MPEG_OIV6_SRC="${VCM_TESTDATA}/mpeg-oiv6"

CONF_NAME="eval_cfp_codec"

CMD="compressai-vision-eval"

echo "running detection task with qp=${QP}" 
${CMD} --config-name=${CONF_NAME}.yaml ${CODEC_PARAMS} \
        ++paths._run_root=${OUTPUT_DIR} \
        ++pipeline.type=image \
        ++pipeline.conformance.save_conformance_files=True \
        ++pipeline.conformance.subsample_ratio=9 \
        ++codec.encoder_config.qp=${QP} \
        ++codec.eval_encode='bpp' \
        ++codec.experiment=${EXPERIMENT} \
        ++vision_model.arch=faster_rcnn_X_101_32x8d_FPN_3x \
        ++dataset.type=Detectron2Dataset \
        ++dataset.datacatalog=MPEGOIV6 \
        ++dataset.config.root=${MPEG_OIV6_SRC} \
        ++dataset.config.annotation_file=annotations/mpeg-oiv6-detection-coco.json \
        ++dataset.config.dataset_name=mpeg-oiv6-detection \
        ++evaluator.type=OIC-EVAL \
        ++misc.device="${DEVICE}"