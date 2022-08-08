# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import six
import os
import numpy as np
# import paddle
from psutil import cpu_count
from paddlenlp.transformers import AutoTokenizer
import fastdeploy


def token_cls_print_ret(infer_result, input_data):
    rets = infer_result["value"]
    for i, ret in enumerate(rets):
        print("input data:", input_data[i])
        print("The model detects all entities:")
        for iterm in ret:
            print("entity:", iterm["entity"], "  label:", iterm["label"],
                  "  pos:", iterm["pos"])
        print("-----------------------------")


def seq_cls_print_ret(infer_result, input_data):
    label_list = [
        "news_story", "news_culture", "news_entertainment", "news_sports",
        "news_finance", "news_house", "news_car", "news_edu", "news_tech",
        "news_military", "news_travel", "news_world", "news_stock",
        "news_agriculture", "news_game"
    ]
    label = infer_result["label"].squeeze().tolist()
    confidence = infer_result["confidence"].squeeze().tolist()
    for i, ret in enumerate(infer_result):
        print("input data:", input_data[i])
        print("seq cls result:")
        print("label:", label_list[label[i]], "  confidence:", confidence[i])
        print("-----------------------------")


class ErniePredictor(object):
    def __init__(self, args):
        if not isinstance(args.device, six.string_types):
            print(
                ">>> [InferBackend] The type of device must be string, but the type you set is: ",
                type(device))
            exit(0)
        args.device = args.device.lower()
        if args.device not in ['cpu', 'gpu', 'xpu']:
            print(
                ">>> [InferBackend] The device must be cpu or gpu, but your device is set to:",
                type(args.device))
            exit(0)

        self.task_name = args.task_name
        self.tokenizer = AutoTokenizer.from_pretrained(
            args.model_name_or_path, use_faster=True)
        if args.task_name == 'seq_cls':
            self.label_names = []
            self.preprocess = self.seq_cls_preprocess
            self.postprocess = self.seq_cls_postprocess
            self.printer = seq_cls_print_ret
        elif args.task_name == 'token_cls':
            self.label_names = [
                'O', 'B-PER', 'I-PER', 'B-ORG', 'I-ORG', 'B-LOC', 'I-LOC'
            ]
            self.preprocess = self.token_cls_preprocess
            self.postprocess = self.token_cls_postprocess
            self.printer = token_cls_print_ret
        else:
            print(
                "[ErniePredictor]: task_name only support seq_cls and token_cls now."
            )
            exit(0)

        self.max_seq_length = args.max_seq_length

        if args.device == 'cpu':
            args.set_dynamic_shape = False
            args.shape_info_file = None
            args.batch_size = 32
        if args.device == 'gpu':
            args.num_threads = cpu_count(logical=False)
        # Set the runtime option
        runtime_option = fastdeploy.RuntimeOption()
        runtime_option.set_model_path(args.model_path + ".pdmodel",
                                      args.model_path + ".pdiparams")
        precision_mode = args.precision_mode.lower()
        use_fp16 = precision_mode == "fp16"
        # runtime_option.use_paddle_backend()
        if args.device == 'cpu':
            runtime_option.use_cpu()
            runtime_option.set_cpu_thread_num(args.num_threads)
            if use_fp16:
                runtime_option.enable_paddle_mkldnn()
        elif args.device == 'gpu':
            runtime_option.use_gpu()
            if use_fp16:
                runtime_option.use_trt_backend()
                runtime_option.enable_trt_fp16()

        self.inference_backend = fastdeploy.Runtime(runtime_option._option)
        if args.set_dynamic_shape:
            # If set_dynamic_shape is turned on, all required dynamic shapes will be
            # automatically set according to the batch_size and max_seq_length.
            self.set_dynamic_shape(args.max_seq_length, args.batch_size)
            exit(0)

    def seq_cls_preprocess(self, input_data: list):
        data = input_data
        # tokenizer + pad
        data = self.tokenizer(
            data,
            max_length=self.max_seq_length,
            padding=True,
            truncation=True)
        input_ids = data["input_ids"]
        token_type_ids = data["token_type_ids"]
        return {
            "input_ids": np.array(
                input_ids, dtype="int64"),
            "token_type_ids": np.array(
                token_type_ids, dtype="int64")
        }

    def seq_cls_postprocess(self, infer_data, input_data):
        logits = np.array(infer_data[0])
        max_value = np.max(logits, axis=1, keepdims=True)
        exp_data = np.exp(logits - max_value)
        probs = exp_data / np.sum(exp_data, axis=1, keepdims=True)
        out_dict = {
            "label": probs.argmax(axis=-1),
            "confidence": probs.max(axis=-1)
        }
        return out_dict

    def token_cls_preprocess(self, data: list):
        # tokenizer + pad
        is_split_into_words = False
        if isinstance(data[0], list):
            is_split_into_words = True
        data = self.tokenizer(
            data,
            max_length=self.max_seq_length,
            padding=True,
            truncation=True,
            is_split_into_words=is_split_into_words)

        input_ids = data["input_ids"]
        token_type_ids = data["token_type_ids"]
        return {
            "input_ids": np.array(
                input_ids, dtype="int64"),
            "token_type_ids": np.array(
                token_type_ids, dtype="int64")
        }

    def token_cls_postprocess(self, infer_data, input_data):
        result = np.array(infer_data[0])
        tokens_label = result.argmax(axis=-1).tolist()
        # 获取batch中每个token的实体
        value = []
        for batch, token_label in enumerate(tokens_label):
            start = -1
            label_name = ""
            items = []
            for i, label in enumerate(token_label):
                if (self.label_names[label] == "O" or
                        "B-" in self.label_names[label]) and start >= 0:
                    entity = input_data[batch][start:i - 1]
                    if isinstance(entity, list):
                        entity = "".join(entity)
                    items.append({
                        "pos": [start, i - 2],
                        "entity": entity,
                        "label": label_name,
                    })
                    start = -1
                if "B-" in self.label_names[label]:
                    start = i - 1
                    label_name = self.label_names[label][2:]
            if start >= 0:
                items.append({
                    "pos": [start, len(token_label) - 1],
                    "entity": input_data[batch][start:len(token_label) - 1],
                    "label": ""
                })
            value.append(items)

        out_dict = {"value": value, "tokens_label": tokens_label}
        return out_dict

    def set_dynamic_shape(self, max_seq_length, batch_size):
        # The dynamic shape info required by TRT is automatically generated
        # according to max_seq_length and batch_size and stored in shape_info.txt
        min_batch_size, max_batch_size, opt_batch_size = 1, batch_size, batch_size
        min_seq_len, max_seq_len, opt_seq_len = 2, max_seq_length, max_seq_length
        batches = [
            {
                "input_ids": np.zeros(
                    [min_batch_size, min_seq_len], dtype="int64"),
                "token_type_ids": np.zeros(
                    [min_batch_size, min_seq_len], dtype="int64")
            },
            {
                "input_ids": np.zeros(
                    [max_batch_size, max_seq_len], dtype="int64"),
                "token_type_ids": np.zeros(
                    [max_batch_size, max_seq_len], dtype="int64")
            },
            {
                "input_ids": np.zeros(
                    [opt_batch_size, opt_seq_len], dtype="int64"),
                "token_type_ids": np.zeros(
                    [opt_batch_size, opt_seq_len], dtype="int64")
            },
        ]
        for batch in batches:
            self.inference_backend.infer(batch)
        print(
            "[InferBackend] Set dynamic shape finished, please close set_dynamic_shape and restart."
        )

    def infer(self, data):
        return self.inference_backend.infer(data)

    def predict(self, input_data: list):
        preprocess_result = self.preprocess(input_data)
        infer_result = self.infer(preprocess_result)
        result = self.postprocess(infer_result, input_data)
        self.printer(result, input_data)
        return result